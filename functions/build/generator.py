"""Code generation, structure validation, and repair engine for AI Build Mode."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from functions.build.config import BuildConfig
from functions.helpers import ParadiseError

if TYPE_CHECKING:
    from functions.ai import AIService

log = logging.getLogger("paradise.build.generator")

# System prompt for initial project generation
BUILDER_SYSTEM_PROMPT = """You are ParadiseBOT's elite AI Software Architect and Frontend Engineer.
You build fully working, beautiful, interactive, production-ready web applications based on user requests.

CRITICAL ARCHITECTURAL REQUIREMENTS:
1. Deliver COMPLETE, WORKING CODE. Never output placeholders, stubs, "TODO", or "...rest of code".
2. The user will preview this directly in a web browser. It MUST WORK OUT OF THE BOX.
3. For single-page apps, you can provide modern HTML5, modern CSS3 (or Tailwind CSS loaded via CDN `<script src="https://cdn.tailwindcss.com"></script>`), and interactive vanilla JavaScript or React 18 (loaded via CDN with Babel standalone).
4. Responsive design: The application MUST look stunning on both mobile screens and desktop monitors.
5. Rich interactivity: Buttons, modals, tabs, storage, calculations, keyboard navigation, or animations must function realistically.
6. Local storage: Persist state (e.g. todo items, notes, high scores, settings) in `localStorage` so refreshes keep user data.
7. Tone and styling: Modern, clean, dark-mode inspired or beautifully vibrant, with smooth transitions, clean typography, and polish.
8. NEVER include backend server calls to non-existent localhost ports. Use mock data, in-memory state, or browser localStorage.

OUTPUT FORMAT:
You MUST respond with a strict JSON object containing:
{
  "title": "Short Project Title",
  "framework": "html" (or "react"),
  "description": "A 1-2 sentence description of what was built.",
  "files": {
    "index.html": "<full file contents>",
    "styles.css": "<optional css file contents>",
    "app.js": "<optional js file contents>"
  }
}
Output ONLY the JSON object. Do not include markdown code block backticks outside the JSON or conversational chatter.
Ensure "index.html" is always present in "files".
"""

# System prompt for iterative project modifications
EDITOR_SYSTEM_PROMPT = """You are ParadiseBOT's elite AI Software Architect.
The user wants to iteratively modify or improve an existing web application project they previously built with you.

You are given:
1. The project title and description.
2. The existing project files and their current contents.
3. The user's requested changes.

CRITICAL MODIFICATION RULES:
1. PRESERVE existing functionality and design unless specifically asked to change or improve them.
2. Do NOT wipe out existing features. Build on top of the existing source code.
3. Update existing files or introduce new files as appropriate.
4. Ensure the modified code remains completely functional and bug-free.
5. All buttons, styling, and interactions must work cleanly in a browser preview.

OUTPUT FORMAT:
Respond with a strict JSON object containing:
{
  "title": "Updated or current title",
  "description": "Summary of changes made",
  "files": {
    "index.html": "<full updated content of index.html>",
    ... any other updated or new files ...
  }
}
Output ONLY the JSON object. Do not include extraneous markdown commentary outside the JSON.
"""

# System prompt for repairing compilation or runtime/syntax issues
REPAIR_SYSTEM_PROMPT = """You are an automated Code Repair Agent.
A generated web project had validation or syntax issues.
Fix the issues and return the corrected complete files in the same JSON format:
{
  "title": "Project Title",
  "description": "Fixed issues description",
  "files": {
    "index.html": "...",
    ...
  }
}
Output ONLY the valid JSON object.
"""


@dataclass
class GeneratedProject:
    title: str
    description: str
    framework: str
    files: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class BuildGenerator:
    """Multi-step code generator and validator."""

    def __init__(self, ai: AIService, config: BuildConfig) -> None:
        self.ai = ai
        self.config = config

    def _extract_json(self, raw_text: str) -> dict[str, Any] | None:
        """Extract and parse JSON safely from model response."""
        text = raw_text.strip()
        # Remove leading/trailing markdown blocks if present
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        # Try direct parse
        try:
            return json.loads(text)
        except Exception:
            pass

        # Try to find JSON object bounds { ... }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = text[start : end + 1]
            try:
                return json.loads(snippet)
            except Exception:
                pass

        # If raw text contains html directly, wrap it in fallback format
        if "<!DOCTYPE html" in text or "<html" in text:
            return {
                "title": "Web Application",
                "framework": "html",
                "description": "Generated web application",
                "files": {"index.html": text},
            }

        return None

    def _validate_project(self, data: dict[str, Any]) -> tuple[bool, list[str]]:
        """Validate generated project structure and files."""
        issues: list[str] = []
        files = data.get("files")
        if not isinstance(files, dict) or not files:
            issues.append("Project does not contain any files.")
            return False, issues

        # Check for index.html
        has_index = False
        for path in files.keys():
            clean_path = path.replace("\\", "/").strip("/")
            if clean_path.casefold() in ("index.html", "index.htm"):
                has_index = True
                break

        if not has_index:
            issues.append("Missing mandatory 'index.html' entry point.")

        # Check file sizes and contents
        for filename, content in files.items():
            if not isinstance(content, str):
                issues.append(f"File {filename} content is not text.")
                continue
            if len(content.encode("utf-8")) > self.config.max_file_size_bytes:
                issues.append(f"File {filename} exceeds maximum allowed size.")

        return len(issues) == 0, issues

    async def _call_ai_coder(self, messages: list[dict], max_tokens: int = 3500) -> str:
        """Execute chat completion, preferring coder/deep model role."""
        # Try role 'coder' first; if not configured, AIService will use deep role
        return await self.ai.chat(messages, max_tokens=max_tokens, role="coder")

    async def generate_project(self, prompt: str, user_name: str = "User") -> GeneratedProject:
        """Perform initial project generation."""
        user_message = f"Please build the following project for {user_name}:\n{prompt}"
        messages = [
            {"role": "system", "content": BUILDER_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        log.info("Generating project for prompt: %r", prompt[:80])
        raw_reply = await self._call_ai_coder(messages, max_tokens=3500)
        parsed = self._extract_json(raw_reply)

        if not parsed:
            # Retry once with structured request
            log.warning("Initial generation output was not valid JSON, retrying...")
            retry_messages = messages + [
                {"role": "assistant", "content": raw_reply[:500]},
                {"role": "user", "content": "Your response must be valid JSON matching the specified format with an index.html file."},
            ]
            raw_reply = await self._call_ai_coder(retry_messages, max_tokens=3500)
            parsed = self._extract_json(raw_reply)

        if not parsed:
            raise ParadiseError("I was unable to structure the project code properly. Please try again.")

        valid, issues = self._validate_project(parsed)
        if not valid:
            log.warning("Project validation issues: %s — attempting repair", issues)
            parsed = await self.repair_project(parsed, issues)
            valid, issues = self._validate_project(parsed)
            if not valid:
                raise ParadiseError(f"Generated project had build errors: {', '.join(issues)}")

        files = parsed.get("files", {})
        # Normalize files dictionary
        clean_files = {k.replace("\\", "/").strip("/"): v for k, v in files.items()}

        return GeneratedProject(
            title=str(parsed.get("title") or "Interactive Web Project")[:60],
            description=str(parsed.get("description") or "Generated with ParadiseBOT AI Build Mode")[:200],
            framework=str(parsed.get("framework") or "html").lower(),
            files=clean_files,
            warnings=issues,
        )

    async def modify_project(
        self,
        current_title: str,
        current_description: str,
        current_files: dict[str, str],
        edit_instruction: str,
        user_name: str = "User",
    ) -> GeneratedProject:
        """Perform iterative modification of an existing project."""
        files_summary = "\n\n".join(
            f"--- FILE: {fname} ---\n{content}" for fname, content in current_files.items()
        )

        user_content = (
            f"Project: {current_title}\n"
            f"Current description: {current_description}\n\n"
            f"EXISTING SOURCE FILES:\n{files_summary}\n\n"
            f"USER EDIT REQUEST:\n{edit_instruction}\n\n"
            f"Apply the edits, improve the app, and return the complete updated files."
        )

        messages = [
            {"role": "system", "content": EDITOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        log.info("Modifying project %r with edit: %r", current_title, edit_instruction[:80])
        raw_reply = await self._call_ai_coder(messages, max_tokens=3500)
        parsed = self._extract_json(raw_reply)

        if not parsed:
            log.warning("Modification output was not valid JSON, retrying...")
            retry_messages = messages + [
                {"role": "assistant", "content": raw_reply[:500]},
                {"role": "user", "content": "Please reply with valid JSON only, retaining index.html and all necessary files."},
            ]
            raw_reply = await self._call_ai_coder(retry_messages, max_tokens=3500)
            parsed = self._extract_json(raw_reply)

        if not parsed:
            raise ParadiseError("Could not apply edits cleanly to the project. Please try rephrasing your request.")

        # Merge updated files onto current files so partial returns don't lose unchanged assets
        updated_files = dict(current_files)
        new_files = parsed.get("files", {})
        if isinstance(new_files, dict):
            for k, v in new_files.items():
                clean_k = k.replace("\\", "/").strip("/")
                updated_files[clean_k] = v

        parsed["files"] = updated_files

        valid, issues = self._validate_project(parsed)
        if not valid:
            parsed = await self.repair_project(parsed, issues)

        return GeneratedProject(
            title=str(parsed.get("title") or current_title)[:60],
            description=str(parsed.get("description") or f"Updated: {edit_instruction}")[:200],
            framework="html",
            files=parsed["files"],
        )

    async def repair_project(self, project_data: dict[str, Any], issues: list[str]) -> dict[str, Any]:
        """Attempt to automatically fix invalid project files."""
        log.info("Repairing project with issues: %s", issues)
        messages = [
            {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"The following project JSON had issues: {', '.join(issues)}\n\nProject Data:\n{json.dumps(project_data)[:3000]}\n\nFix all issues and output corrected JSON.",
            },
        ]
        try:
            raw = await self._call_ai_coder(messages, max_tokens=3500)
            repaired = self._extract_json(raw)
            if repaired and isinstance(repaired.get("files"), dict):
                return repaired
        except Exception as e:
            log.warning("Repair pass failed: %s", e)

        # Fallback synthetic fix if index.html is missing
        files = project_data.get("files", {})
        if not any(k.casefold() in ("index.html", "index.htm") for k in files):
            # If any other HTML exists, alias it to index.html
            html_keys = [k for k in files if k.endswith(".html")]
            if html_keys:
                files["index.html"] = files[html_keys[0]]
            else:
                files["index.html"] = "<!DOCTYPE html><html><body><h1>Project Ready</h1></body></html>"
            project_data["files"] = files

        return project_data
