# Paradise Bot

Paradise is a small Discord bot for one private server. It comes online, quietly changes what it is doing, keeps a few daily notes, and answers one health command. That is all Phase 1 is meant to do.

## Requirements

- Node.js 20 or newer
- A Discord application with a bot added to the Paradise server
- The `bot` and `applications.commands` OAuth scopes

Paradise only requests the `Guilds` gateway intent. No privileged intents are needed.

## Setup

Install dependencies:

```bash
npm install
```

Copy the example environment file:

```bash
cp .env.example .env
```

Fill in all three values:

```env
DISCORD_TOKEN=
CLIENT_ID=
GUILD_ID=
```

`CLIENT_ID` is the Discord application ID. `GUILD_ID` is the ID of the private server. Keep `.env` private; it is ignored by Git.

Start Paradise:

```bash
npm start
```

A successful connection prints:

```text
🌴 Paradise is awake.
```

The guild-scoped `/ping` command is registered at startup and replies with `alive unfortunately.`

## Presence

Activities live in [`src/presence/activities.js`](src/presence/activities.js). They are grouped into normal, gaming, weird, sleepy, chaotic, mysterious, server, and pointless categories.

Paradise picks across those categories and waits a random 20–60 minutes before considering another activity. It avoids an immediate repeat and sometimes leaves the current activity alone. Add a short activity to the appropriate category to extend the pool.

## Daily notes

Daily note text lives in [`src/personality/notes.js`](src/personality/notes.js). Paradise selects between one and four unique notes for each UTC day. It shows one as a Discord custom status at startup, returns to the day's set occasionally during activity rotation, and shows a new set after the next UTC midnight.

The current set is saved in `data/daily-notes.json`, so restarting on the same day does not choose new notes. The runtime file is ignored by Git. On a host with an ephemeral filesystem, persist or mount the `data` directory if notes must survive deployments.

## Checks

```bash
npm test
```
