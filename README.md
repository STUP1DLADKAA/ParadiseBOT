# Paradise Bot

A small private Discord bot for a friends-only server. Paradise has slash commands, quiet conversational reactions, in-memory reminders/AFK state, and a slowly changing presence. It is intentionally not a moderation suite, dashboard, or AI wrapper.

## Run it

Requires Python 3.11 or newer.

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. On the bot page, enable the **Message Content Intent**. Paradise uses it for rare reactions, AFK clearing, and conversational responses.
3. Invite it with the `bot` and `applications.commands` scopes. Useful permissions are View Channels, Send Messages, Embed Links, Add Reactions, Read Message History, and Use External Emojis.
4. Install and configure:

```bash
python -m pip install -r requirements.txt
cp .env.example .env
# Put the bot token after DISCORD_TOKEN= in .env
python bot.py
```

The only required setting is:

```env
DISCORD_TOKEN=your_token_here
```

Slash commands are synced globally at startup. Discord can take a little while to show newly registered global commands.

## Commands

- Fun: `/ping`, `/8ball`, `/coinflip`, `/roll`, `/choose`
- Social: `/truth`, `/dare`, `/rate`, `/ship`, `/vibe`
- Useful: `/remind`, `/poll`, `/afk`, `/userinfo`, `/serverinfo`

Choices for `/choose` and `/poll` are separated with `|`, for example:

```text
/choose options:pizza | burgers | something responsible
```

Reminders and AFK states live only in memory and disappear when the bot restarts. Limits and per-user command cooldowns keep temporary state and accidental spam under control.

## Presence note

Discord gives bot accounts one visible activity slot. Paradise maintains separate custom-status and activity rotations, and the two loops take turns using that slot. Activities rotate every 20–60 minutes; custom-style statuses change much less often. Neither pool immediately repeats itself.
