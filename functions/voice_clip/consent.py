"""Recording transparency notice and participant consent management."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import discord

log = logging.getLogger(__name__)

MAX_CONSENT_ENTRIES_PER_GUILD = 2048


def build_recording_notice_text(
    *,
    max_seconds: int = 180,
    consent_mode: str = "notice",
) -> str:
    """Clear, participant-facing recording transparency and consent notice."""
    minutes = max_seconds // 60
    rem = max_seconds % 60
    window_label = f"{minutes}m" if rem == 0 and minutes > 0 else f"{max_seconds}s"
    if consent_mode == "opt_in":
        return (
            f"🔴 **Voice Clip Buffer Active ({window_label} volatile window · Opt-In Mode)**\n"
            f"-# Paradise maintains an in-memory rolling buffer of up to **{max_seconds}s** "
            "of audio in this channel (older audio is discarded automatically; nothing is "
            "archived). Only voices of participants who click **Allow Recording** below are "
            "buffered. Click **Do Not Record Me** anytime to withdraw consent and purge your "
            "buffered audio immediately."
        )
    return (
        f"🔴 **Voice Clip Buffer Active ({window_label} volatile rolling window)**\n"
        f"-# Paradise is undeafened and holds up to the latest **{max_seconds}s** of audio "
        "in volatile memory in this channel (older audio is discarded automatically; nothing "
        "is permanently archived). Speaking unmuted in this channel indicates consent, or "
        "click **Do Not Record Me** below at any time to opt out and immediately purge your "
        "buffered audio."
    )


class ConsentManager:
    """Thread-safe per-guild voice recording consent tracker."""

    def __init__(
        self,
        *,
        mode: str = "notice",
        on_revoke: Callable[[int, int], None] | None = None,
    ) -> None:
        normalized = (mode or "notice").strip().lower()
        self.mode: str = normalized if normalized in {"notice", "opt_in"} else "notice"
        self._on_revoke = on_revoke
        self._lock = threading.Lock()
        # guild_id -> set of user_ids who explicitly opted in
        self._opted_in: dict[int, set[int]] = {}
        # guild_id -> set of user_ids who explicitly opted out
        self._opted_out: dict[int, set[int]] = {}

    def can_record(self, guild_id: int, user_id: int) -> bool:
        """Fast thread-safe check called before buffering a speaker packet."""
        if not isinstance(user_id, int) or user_id <= 0:
            return False
        with self._lock:
            opted_out = self._opted_out.get(guild_id)
            if opted_out is not None and user_id in opted_out:
                return False
            if self.mode == "opt_in":
                opted_in = self._opted_in.get(guild_id)
                return bool(opted_in is not None and user_id in opted_in)
            return True

    def allow_user(self, guild_id: int, user_id: int) -> None:
        """Record explicit participant consent to voice buffering."""
        with self._lock:
            opted_out = self._opted_out.get(guild_id)
            if opted_out is not None:
                opted_out.discard(user_id)
            opted_in = self._opted_in.setdefault(guild_id, set())
            if len(opted_in) >= MAX_CONSENT_ENTRIES_PER_GUILD and user_id not in opted_in:
                opted_in.pop()
            opted_in.add(user_id)

    def revoke_user(self, guild_id: int, user_id: int) -> None:
        """Withdraw consent and immediately trigger purge of buffered packets."""
        with self._lock:
            opted_in = self._opted_in.get(guild_id)
            if opted_in is not None:
                opted_in.discard(user_id)
            opted_out = self._opted_out.setdefault(guild_id, set())
            if len(opted_out) >= MAX_CONSENT_ENTRIES_PER_GUILD and user_id not in opted_out:
                opted_out.pop()
            opted_out.add(user_id)
        if self._on_revoke is not None:
            try:
                self._on_revoke(guild_id, user_id)
            except Exception:  # noqa: BLE001
                log.warning("Consent revocation purge callback failed", exc_info=True)

    def clear_guild(self, guild_id: int) -> None:
        """Clear volatile session state if needed on full reset."""
        with self._lock:
            self._opted_in.pop(guild_id, None)
            self._opted_out.pop(guild_id, None)

    def clear_all(self) -> None:
        with self._lock:
            self._opted_in.clear()
            self._opted_out.clear()


class RecordingConsentView(discord.ui.View):
    """Interactive buttons attached to the VC recording transparency notice."""

    def __init__(self, consent: ConsentManager, guild_id: int) -> None:
        super().__init__(timeout=3600)
        self.consent = consent
        self.guild_id = int(guild_id)

    @discord.ui.button(
        label="Allow Recording",
        emoji="✅",
        style=discord.ButtonStyle.secondary,
    )
    async def allow_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.consent.allow_user(self.guild_id, interaction.user.id)
        try:
            await interaction.response.send_message(
                "Your voice consent preference is set to **Allow Recording** for this server.",
                ephemeral=True,
            )
        except discord.DiscordException:
            log.debug("Could not send consent allow confirmation", exc_info=True)

    @discord.ui.button(
        label="Do Not Record Me",
        emoji="🔇",
        style=discord.ButtonStyle.danger,
    )
    async def opt_out_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.consent.revoke_user(self.guild_id, interaction.user.id)
        try:
            await interaction.response.send_message(
                "Your voice will **not** be buffered, and any buffered audio from you has been purged.",
                ephemeral=True,
            )
        except discord.DiscordException:
            log.debug("Could not send consent opt-out confirmation", exc_info=True)
