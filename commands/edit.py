"""Voice data editing command for the dedicated user.

This command allows the authorized user (922359954025885737) to adjust
voice activity time for any user across all time periods (today, yesterday,
week, month, all_time). Changes are applied directly to the voice_daily_stats
table and reflected immediately in /vc leaderboard and /vc stats.

Unauthorized users receive a vague technical error message.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from config import VoiceConfig
from functions.helpers import Colour
from functions.voice.models import VoicePeriod

log = logging.getLogger(__name__)

#: The single Discord user ID authorized to use /edit
AUTHORIZED_EDIT_USER_ID: int = 922359954025885737

#: Vague error message for unauthorized users (looks like a technical issue)
UNAUTHORIZED_EDIT_MESSAGE: str = (
    "something went wrong trying to process that request. "
    "try again in a moment."
)


def _is_authorized(user_id: int | None) -> bool:
    """Check if the user is the authorized editor."""
    return user_id is not None and user_id == AUTHORIZED_EDIT_USER_ID


def _format_seconds(total_seconds: int) -> str:
    """Format seconds into human-readable duration."""
    total_seconds = max(0, int(total_seconds))
    if total_seconds == 0:
        return "0m"

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or (hours > 0 and seconds > 0):
        parts.append(f"{minutes}m")
    if not parts or (hours == 0 and minutes == 0):
        parts.append(f"{seconds}s")

    return " ".join(parts)


def _format_adjustment(seconds: int) -> str:
    """Format an adjustment value (positive or negative)."""
    if seconds > 0:
        return f"+{_format_seconds(seconds)}"
    elif seconds < 0:
        return f"-{_format_seconds(abs(seconds))}"
    return "0s"


class EditVoiceData(commands.Cog):
    """Slash command /edit for adjusting voice activity data."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = getattr(getattr(bot, "config", None), "voice", None) or VoiceConfig()

    async def _get_store(self):
        """Get the voice store from the tracker if available."""
        voice_cog = self.bot.get_cog("Voice") or self.bot.get_cog("vc")
        if voice_cog and hasattr(voice_cog, "store") and voice_cog.store is not None:
            return voice_cog.store
        return None

    async def _get_user_breakdown(
        self, store, guild_id: int, user_id: int
    ) -> dict[str, int]:
        """Get the current voice breakdown for a user."""
        try:
            return await store.get_user_breakdown(guild_id, user_id)
        except Exception:
            return {
                "today": 0,
                "yesterday": 0,
                "week": 0,
                "month": 0,
                "all_time": 0,
            }

    async def _update_daily_stat(
        self,
        store,
        guild_id: int,
        user_id: int,
        date: date,
        delta_seconds: int,
    ) -> bool:
        """Update a daily stat for a specific date.

        Args:
            store: The voice store
            guild_id: The guild ID
            user_id: The user ID
            date: The date to update
            delta_seconds: The amount to add (positive) or subtract (negative)

        Returns:
            True if successful, False otherwise
        """
        try:
            # Get the specific user's daily record
            records = await store.get_daily_guild_user_records(date.isoformat())
            current_value = 0
            for record in records:
                if record.guild_id == guild_id and record.user_id == user_id:
                    current_value = record.duration_seconds
                    break

            # Calculate new value (cannot go below 0)
            new_value = max(0, current_value + delta_seconds)

            # Use the Supabase client directly to update the table
            if hasattr(store, "_execute") and hasattr(store, "_ensure_client"):
                client = await store._ensure_client()

                # Access the voice_daily_stats table and update the row
                result = await store._execute(
                    "edit daily stat",
                    lambda c: c.table("voice_daily_stats")
                    .update({"duration_seconds": new_value, "updated_at": "now()"})
                    .eq("guild_id", guild_id)
                    .eq("user_id", user_id)
                    .eq("date", date.isoformat())
                    .execute(),
                )

                # Check if the update was successful
                if hasattr(result, "data") and result.data:
                    return True
                elif hasattr(result, "count") and result.count > 0:
                    return True
                else:
                    # If no rows were updated, the row might not exist
                    # Try inserting a new row
                    insert_result = await store._execute(
                        "insert daily stat",
                        lambda c: c.table("voice_daily_stats")
                        .insert({
                            "guild_id": guild_id,
                            "user_id": user_id,
                            "date": date.isoformat(),
                            "duration_seconds": new_value,
                        })
                        .execute(),
                    )
                    if hasattr(insert_result, "data") and insert_result.data:
                        return True
                    return False
            else:
                log.warning(
                    "Cannot edit daily stat: store doesn't support direct table access"
                )
                return False
        except Exception as e:
            log.exception(f"Failed to update daily stat: {e}")
            return False

    async def _apply_time_adjustment(
        self,
        store,
        guild_id: int,
        user_id: int,
        period: VoicePeriod,
        delta_seconds: int,
    ) -> tuple[bool, str]:
        """Apply a time adjustment to the specified period.

        For daily periods (today, yesterday), directly updates the daily stat.
        For rolling periods (week, month, all_time), adjusts the underlying
        daily stats.

        Args:
            store: The voice store
            guild_id: The guild ID
            user_id: The user ID
            period: The time period to adjust
            delta_seconds: The amount to adjust (positive or negative)

        Returns:
            Tuple of (success, message)
        """
        try:
            if period == VoicePeriod.TODAY:
                today = date.today()
                success = await self._update_daily_stat(
                    store, guild_id, user_id, today, delta_seconds
                )
                if success:
                    return True, f"✅ Today's voice time adjusted by {_format_adjustment(delta_seconds)}"
                return False, "Failed to update today's stats"

            elif period == VoicePeriod.YESTERDAY:
                yesterday = date.today() - timedelta(days=1)
                success = await self._update_daily_stat(
                    store, guild_id, user_id, yesterday, delta_seconds
                )
                if success:
                    return True, f"✅ Yesterday's voice time adjusted by {_format_adjustment(delta_seconds)}"
                return False, "Failed to update yesterday's stats"

            elif period == VoicePeriod.WEEK:
                # Week = last 7 days including today
                today = date.today()
                total_delta = 0
                for i in range(7):
                    day = today - timedelta(days=i)
                    # Distribute the delta across 7 days proportionally
                    day_delta = delta_seconds // 7
                    remainder = delta_seconds % 7 if i == 0 else 0
                    if day_delta != 0 or remainder != 0:
                        success = await self._update_daily_stat(
                            store, guild_id, user_id, day, day_delta + remainder
                        )
                        if success:
                            total_delta += day_delta + remainder
                if total_delta == delta_seconds:
                    return True, f"✅ This week's voice time adjusted by {_format_adjustment(delta_seconds)}"
                return False, "Failed to update week's stats"

            elif period == VoicePeriod.MONTH:
                # Month = last 30 days including today
                today = date.today()
                total_delta = 0
                for i in range(30):
                    day = today - timedelta(days=i)
                    day_delta = delta_seconds // 30
                    remainder = delta_seconds % 30 if i == 0 else 0
                    if day_delta != 0 or remainder != 0:
                        success = await self._update_daily_stat(
                            store, guild_id, user_id, day, day_delta + remainder
                        )
                        if success:
                            total_delta += day_delta + remainder
                if total_delta == delta_seconds:
                    return True, f"✅ This month's voice time adjusted by {_format_adjustment(delta_seconds)}"
                return False, "Failed to update month's stats"

            elif period == VoicePeriod.ALL_TIME:
                # All time is trickier - we can't easily distribute across all days
                # For now, we'll add a note that this requires manual DB edit
                # Or we could add a large delta to today and let the system handle it
                # Better approach: record it in a way that affects all_time
                # Since all_time is the sum of all daily stats, we need to add
                # the delta to a specific day (e.g., today)
                success = await self._update_daily_stat(
                    store, guild_id, user_id, date.today(), delta_seconds
                )
                if success:
                    return True, f"✅ All time voice time adjusted by {_format_adjustment(delta_seconds)} (added to today)"
                return False, "Failed to update all time stats"

            return False, "Unknown period"

        except Exception as e:
            log.exception(f"Error applying time adjustment: {e}")
            return False, "An error occurred while processing your request"

    @app_commands.command(
        name="edit",
        description="Adjust voice activity time for a user (authorized user only)",
    )
    @app_commands.describe(
        user="The Discord user to adjust time for",
        period="The time period to adjust",
        adjustment="Seconds to add (positive) or subtract (negative)",
    )
    @app_commands.choices(
        period=[
            app_commands.Choice(name="📅 Today", value="today"),
            app_commands.Choice(name="⏪ Yesterday", value="yesterday"),
            app_commands.Choice(name="📊 This Week", value="week"),
            app_commands.Choice(name="🗓️ This Month", value="month"),
            app_commands.Choice(name="👑 All Time", value="all_time"),
        ]
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        period: str,
        adjustment: app_commands.Range[int, -86400, 86400],  # ±24 hours max
    ) -> None:
        """Adjust voice activity time for a user."""
        # Authorization check FIRST
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        if not _is_authorized(user_id):
            log.warning(
                "Denied unauthorized /edit invocation (user_id=%s)", user_id
            )
            await interaction.response.send_message(
                UNAUTHORIZED_EDIT_MESSAGE, ephemeral=True
            )
            return

        # Get the voice store
        store = await self._get_store()
        if store is None:
            await interaction.response.send_message(
                "Voice tracking is not configured. The owner needs to set up Supabase.",
                ephemeral=True,
            )
            return

        # Parse the period
        try:
            voice_period = VoicePeriod(period)
        except ValueError:
            await interaction.response.send_message(
                "Invalid period specified.", ephemeral=True
            )
            return

        # Acknowledge the interaction
        await interaction.response.defer(ephemeral=True)

        # Get current breakdown for display
        current_breakdown = await self._get_user_breakdown(
            store, interaction.guild_id, user.id
        )

        # Apply the adjustment
        success, message = await self._apply_time_adjustment(
            store,
            interaction.guild_id,
            user.id,
            voice_period,
            adjustment,
        )

        if success:
            # Get updated breakdown
            new_breakdown = await self._get_user_breakdown(
                store, interaction.guild_id, user.id
            )

            embed = discord.Embed(
                title=f"🎙️ Voice Time Adjusted for {user.display_name}",
                color=Colour.ACCENT,
            )

            # Show the adjustment
            sign = "+" if adjustment > 0 else ""
            embed.add_field(
                name="Adjustment",
                value=f"**{sign}{adjustment}s** ({_format_seconds(adjustment)})",
                inline=False,
            )

            # Show period
            embed.add_field(
                name="Period",
                value=voice_period.label,
                inline=False,
            )

            # Show new breakdown
            embed.add_field(
                name="New Breakdown",
                value=(
                    f"📅 Today: {_format_seconds(new_breakdown['today'])}s\n"
                    f"⏪ Yesterday: {_format_seconds(new_breakdown['yesterday'])}s\n"
                    f"📊 This Week: {_format_seconds(new_breakdown['week'])}s\n"
                    f"🗓️ This Month: {_format_seconds(new_breakdown['month'])}s\n"
                    f"👑 All Time: {_format_seconds(new_breakdown['all_time'])}s"
                ),
                inline=False,
            )

            # Show previous vs new for the adjusted period
            old_value = current_breakdown[period]
            new_value = new_breakdown[period]
            diff = new_value - old_value
            diff_sign = "+" if diff > 0 else ""
            embed.add_field(
                name="Period Change",
                value=(
                    f"Before: {_format_seconds(old_value)}s\n"
                    f"After: {_format_seconds(new_value)}s\n"
                    f"Change: {diff_sign}{diff}s"
                ),
                inline=False,
            )

            embed.set_footer(text=f"User ID: {user.id} | Guild ID: {interaction.guild_id}")

            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(message, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EditVoiceData(bot))
