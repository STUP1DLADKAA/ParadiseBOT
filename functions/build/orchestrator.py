"""Orchestrator and concurrent task queue for AI Build Mode."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Coroutine

import discord

from functions.build.config import BuildConfig
from functions.build.generator import BuildGenerator, GeneratedProject
from functions.build.server import PreviewServer
from functions.build.session import ProjectSession, ProjectStore
from functions.helpers import ParadiseError

if TYPE_CHECKING:
    from functions.ai import AIService

log = logging.getLogger("paradise.build.orchestrator")


# Visual progress stages
BUILD_STAGES = [
    ("Understanding", "🧠 Analyzing project requirements..."),
    ("Planning", "📐 Architecting components and UI layouts..."),
    ("Generating", "⚡ Synthesizing production code and assets..."),
    ("Implementing", "🔨 Assembling project files and styles..."),
    ("Validating", "🧪 Verifying structure and responsiveness..."),
    ("Deploying", "🚀 Publishing to live sandbox preview..."),
    ("Ready", "✨ Project is live!"),
]


@dataclass
class BuildTask:
    user_id: int
    channel_id: int
    prompt: str
    is_edit: bool
    project_id: str | None
    created_at: float
    future: asyncio.Future


class BuildOrchestrator:
    """Manages build concurrency, queuing, project generation, and Discord progress tracking."""

    def __init__(self, bot, ai: AIService, config: BuildConfig) -> None:
        self.bot = bot
        self.ai = ai
        self.config = config
        self.store = ProjectStore(config.db_path, config.projects_dir)
        self.generator = BuildGenerator(ai, config)
        self.preview_server = PreviewServer(config, self.store)

        # Semaphores and active jobs
        self._global_semaphore = asyncio.Semaphore(config.max_concurrent_builds)
        self._user_locks: dict[int, asyncio.Lock] = {}
        self._active_builds: dict[str, float] = {}

    async def start(self) -> None:
        """Initialize preview server and resources."""
        await self.preview_server.start()

    async def stop(self) -> None:
        """Stop preview server."""
        await self.preview_server.stop()

    def _get_user_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._user_locks.get(user_id)
        if lock is None:
            if len(self._user_locks) > 256:
                self._user_locks.clear()
            lock = asyncio.Lock()
            self._user_locks[user_id] = lock
        return lock

    def _format_progress_embed(
        self,
        project_title: str,
        request_desc: str,
        current_stage_idx: int,
        elapsed_sec: float,
        is_edit: bool = False,
    ) -> discord.Embed:
        """Build an embed showing progress through stages."""
        color = 0x38BDF8  # Bright celestial sky blue
        embed = discord.Embed(
            title=f"🔨 {'Updating' if is_edit else 'Building'}: {project_title}",
            description=f"**Prompt:** {request_desc[:200]}",
            color=color,
        )

        stage_lines = []
        for idx, (name, desc) in enumerate(BUILD_STAGES):
            if idx < current_stage_idx:
                stage_lines.append(f"✅ **{name}**")
            elif idx == current_stage_idx:
                stage_lines.append(f"🔄 **{name}** — *{desc}*")
            else:
                stage_lines.append(f"⚪ {name}")

        embed.add_field(
            name="Build Pipeline",
            value="\n".join(stage_lines),
            inline=False,
        )

        progress_pct = int(((current_stage_idx + 1) / len(BUILD_STAGES)) * 100)
        embed.set_footer(text=f"Stage {current_stage_idx + 1}/{len(BUILD_STAGES)} ({progress_pct}%) • Elapsed: {elapsed_sec:.1f}s")
        return embed

    def _format_ready_embed(
        self,
        session: ProjectSession,
        duration: float,
        is_edit: bool = False,
    ) -> discord.Embed:
        """Polished embed when build completes successfully."""
        color = 0x10B981  # Emerald green
        title = f"✨ {'Project Updated' if is_edit else 'Project Ready'}: {session.title}"
        embed = discord.Embed(
            title=title,
            description=session.description,
            color=color,
            url=session.preview_url,
        )

        embed.add_field(
            name="🌐 Live Preview",
            value=f"**[Open Live Preview]({session.preview_url})**\n`{session.preview_url}`",
            inline=False,
        )

        files_list = ", ".join(f"`{f}`" for f in sorted(session.files.keys())[:8])
        if len(session.files) > 8:
            files_list += f" and {len(session.files) - 8} more"

        embed.add_field(
            name="📁 Project Files",
            value=files_list or "`index.html`",
            inline=True,
        )

        exp_mins = int(self.config.preview_expiration_seconds / 60)
        embed.add_field(
            name="⏱️ Build Stats",
            value=f"Duration: `{duration:.1f}s`\nRevision: `#{session.build_count}`\nActive: `{exp_mins} mins`",
            inline=True,
        )

        embed.set_footer(
            text="💡 Tip: Mention me again to modify this project (e.g. '@ParadiseBOT make the background black')"
        )
        return embed

    async def _safe_edit_message(
        self, message: discord.Message, embed: discord.Embed
    ) -> None:
        """Safely edit Discord message with rate-limit and error tolerance."""
        try:
            await message.edit(embed=embed)
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            log.warning("Could not update build progress embed: %s", e)

    async def handle_new_build(
        self,
        trigger_message: discord.Message,
        prompt: str,
    ) -> bool:
        """Process a brand-new project build request."""
        user = trigger_message.author
        channel = trigger_message.channel

        if not self.config.enabled:
            await trigger_message.reply("AI Build Mode is currently disabled by administrator.")
            return True

        if not self.ai.available:
            await trigger_message.reply("AI reasoning service is not currently configured.")
            return True

        # Generate unique project id
        project_id = f"proj-{secrets.token_hex(4)}"
        initial_title = prompt[:40].strip().title() or "Web Application"

        # Acknowledge immediately with stage 0 embed
        start_time = time.monotonic()
        embed = self._format_progress_embed(initial_title, prompt, 0, 0.0, is_edit=False)
        try:
            status_message = await trigger_message.reply(embed=embed)
        except discord.HTTPException:
            # Fallback if reply cannot send embed
            status_message = await channel.send(embed=embed)

        user_lock = self._get_user_lock(user.id)
        if user_lock.locked():
            await self._safe_edit_message(
                status_message,
                discord.Embed(
                    title="⏳ Build Queued",
                    description="You already have a build running. Your request is queued and will begin as soon as it finishes.",
                    color=0xF59E0B,
                ),
            )

        async with user_lock:
            async with self._global_semaphore:
                return await self._execute_build_pipeline(
                    trigger_message=trigger_message,
                    status_message=status_message,
                    project_id=project_id,
                    prompt=prompt,
                    initial_title=initial_title,
                    start_time=start_time,
                    is_edit=False,
                )

    async def handle_edit_build(
        self,
        trigger_message: discord.Message,
        session: ProjectSession,
        edit_instruction: str,
    ) -> bool:
        """Process an iterative modification to an existing project."""
        user = trigger_message.author
        channel = trigger_message.channel

        # Security check: verify project ownership
        if session.owner_id != user.id and not getattr(self.bot.config, "is_lord", lambda x: False)(user.id):
            await trigger_message.reply("You do not have permission to modify someone else's project.")
            return True

        start_time = time.monotonic()
        embed = self._format_progress_embed(
            session.title, edit_instruction, 0, 0.0, is_edit=True
        )
        try:
            status_message = await trigger_message.reply(embed=embed)
        except discord.HTTPException:
            status_message = await channel.send(embed=embed)

        user_lock = self._get_user_lock(user.id)
        async with user_lock:
            async with self._global_semaphore:
                return await self._execute_build_pipeline(
                    trigger_message=trigger_message,
                    status_message=status_message,
                    project_id=session.project_id,
                    prompt=edit_instruction,
                    initial_title=session.title,
                    start_time=start_time,
                    is_edit=True,
                    existing_session=session,
                )

    async def _execute_build_pipeline(
        self,
        trigger_message: discord.Message,
        status_message: discord.Message,
        project_id: str,
        prompt: str,
        initial_title: str,
        start_time: float,
        is_edit: bool,
        existing_session: ProjectSession | None = None,
    ) -> bool:
        """Run through all 7 stages: Understanding → Planning → Generating → Implementing → Validating → Deploying → Ready."""
        user = trigger_message.author
        channel = trigger_message.channel
        user_name = getattr(user, "display_name", None) or "User"

        try:
            # Stage 0: Understanding
            await asyncio.sleep(0.5)
            # Stage 1: Planning
            elapsed = time.monotonic() - start_time
            await self._safe_edit_message(
                status_message,
                self._format_progress_embed(initial_title, prompt, 1, elapsed, is_edit=is_edit),
            )
            await asyncio.sleep(0.5)

            # Stage 2: Generating
            elapsed = time.monotonic() - start_time
            await self._safe_edit_message(
                status_message,
                self._format_progress_embed(initial_title, prompt, 2, elapsed, is_edit=is_edit),
            )

            # Perform AI code generation with timeout
            async def run_gen():
                if is_edit and existing_session:
                    return await self.generator.modify_project(
                        current_title=existing_session.title,
                        current_description=existing_session.description,
                        current_files=existing_session.files,
                        edit_instruction=prompt,
                        user_name=user_name,
                    )
                else:
                    return await self.generator.generate_project(prompt, user_name=user_name)

            generated: GeneratedProject = await asyncio.wait_for(
                run_gen(),
                timeout=self.config.build_timeout,
            )

            # Stage 3: Implementing
            elapsed = time.monotonic() - start_time
            await self._safe_edit_message(
                status_message,
                self._format_progress_embed(generated.title, prompt, 3, elapsed, is_edit=is_edit),
            )
            await asyncio.sleep(0.5)

            # Stage 4: Validating
            elapsed = time.monotonic() - start_time
            await self._safe_edit_message(
                status_message,
                self._format_progress_embed(generated.title, prompt, 4, elapsed, is_edit=is_edit),
            )
            await asyncio.sleep(0.5)

            # Stage 5: Deploying
            elapsed = time.monotonic() - start_time
            await self._safe_edit_message(
                status_message,
                self._format_progress_embed(generated.title, prompt, 5, elapsed, is_edit=is_edit),
            )

            # Persist project session
            now = time.time()
            preview_url = self.config.get_preview_url(project_id)

            if is_edit and existing_session:
                new_history = list(existing_session.build_history)
                new_history.append({
                    "timestamp": now,
                    "action": "edit",
                    "prompt": prompt,
                    "duration": time.monotonic() - start_time,
                })
                session = ProjectSession(
                    project_id=project_id,
                    owner_id=user.id,
                    channel_id=channel.id,
                    title=generated.title,
                    description=generated.description,
                    framework=generated.framework,
                    preview_url=preview_url,
                    created_at=existing_session.created_at,
                    updated_at=now,
                    build_count=existing_session.build_count + 1,
                    deployment_status="ready",
                    files=generated.files,
                    build_history=new_history,
                )
            else:
                session = ProjectSession(
                    project_id=project_id,
                    owner_id=user.id,
                    channel_id=channel.id,
                    title=generated.title,
                    description=generated.description,
                    framework=generated.framework,
                    preview_url=preview_url,
                    created_at=now,
                    updated_at=now,
                    build_count=1,
                    deployment_status="ready",
                    files=generated.files,
                    build_history=[{
                        "timestamp": now,
                        "action": "create",
                        "prompt": prompt,
                        "duration": time.monotonic() - start_time,
                    }],
                )

            self.store.save_project(session)

            # Stage 6: Ready
            total_duration = time.monotonic() - start_time
            ready_embed = self._format_ready_embed(session, total_duration, is_edit=is_edit)
            await self._safe_edit_message(status_message, ready_embed)
            log.info("Build completed for project %s (%s) in %.2fs", project_id, session.title, total_duration)
            return True

        except asyncio.TimeoutError:
            log.warning("Build timed out for user %d (prompt: %r)", user.id, prompt[:60])
            err_embed = discord.Embed(
                title="⚠️ Build Timed Out",
                description="The build took longer than expected to complete. Try a more specific or concise prompt.",
                color=0xEF4444,
            )
            await self._safe_edit_message(status_message, err_embed)
            return True

        except ParadiseError as pe:
            log.warning("Build failed with user error: %s", pe)
            err_embed = discord.Embed(
                title="⚠️ Build Interrupted",
                description=str(pe),
                color=0xEF4444,
            )
            await self._safe_edit_message(status_message, err_embed)
            return True

        except Exception as e:
            log.exception("Unexpected error in build pipeline for user %d", user.id)
            err_embed = discord.Embed(
                title="❌ Build Error",
                description="An unexpected error occurred while constructing the project. The details have been logged.",
                color=0xEF4444,
            )
            await self._safe_edit_message(status_message, err_embed)
            return True
