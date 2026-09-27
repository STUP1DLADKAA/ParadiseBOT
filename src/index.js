import { config as loadEnvironment } from 'dotenv';
import {
  Client,
  Events,
  GatewayIntentBits,
} from 'discord.js';
import { loadConfig } from './config.js';
import { handlePing } from './commands/ping.js';
import { registerCommands } from './commands/registerCommands.js';
import { DailyNotes } from './personality/dailyNotes.js';
import { PresenceManager } from './presence/presenceManager.js';
import { createLogger } from './utils/logger.js';

loadEnvironment({ quiet: true });

const startupLogger = createLogger();

let config;
try {
  config = loadConfig();
} catch (error) {
  startupLogger.error('Configuration error.', error);
  process.exitCode = 1;
}

if (config) {
  const logger = createLogger({ secrets: [config.discordToken] });
  const client = new Client({ intents: [GatewayIntentBits.Guilds] });
  const dailyNotes = new DailyNotes({ logger });
  const presence = new PresenceManager(client, { logger });
  let shuttingDown = false;

  async function shutdown(reason, exitCode = 0) {
    if (shuttingDown) {
      return;
    }

    shuttingDown = true;
    process.exitCode = exitCode;
    logger.info(`Going offline (${reason}).`);

    presence.stop();
    dailyNotes.stop();
    client.destroy();

    const forcedExit = setTimeout(() => process.exit(exitCode), 5_000);
    forcedExit.unref();
  }

  process.once('SIGINT', () => void shutdown('SIGINT'));
  process.once('SIGTERM', () => void shutdown('SIGTERM'));

  process.on('unhandledRejection', (reason) => {
    logger.throttledError(
      'unhandled-rejection',
      'An unexpected promise rejection was caught.',
      reason,
    );
  });

  process.on('uncaughtException', (error) => {
    logger.error('An unexpected exception was caught; restarting is safest.', error);
    void shutdown('unexpected exception', 1);
  });

  client.on(Events.Error, (error) => {
    logger.throttledError('discord-client', 'Discord client error.', error);
  });

  client.on(Events.Warn, (message) => {
    logger.throttledWarn('discord-warning', `Discord warning: ${message}`);
  });

  client.on(Events.ShardError, (error, shardId) => {
    logger.throttledError(`shard-${shardId}`, `Discord connection error on shard ${shardId}.`, error);
  });

  client.on(Events.ShardDisconnect, (_event, shardId) => {
    logger.throttledWarn(
      `shard-disconnect-${shardId}`,
      `Discord shard ${shardId} disconnected; waiting for discord.js to reconnect.`,
    );
  });

  client.on(Events.Invalidated, () => {
    logger.error('The Discord session was invalidated; restarting is safest.');
    void shutdown('invalid Discord session', 1);
  });

  client.on(Events.InteractionCreate, (interaction) => {
    if (!interaction.isChatInputCommand() || interaction.commandName !== 'ping') {
      return;
    }

    void handlePing(interaction).catch((error) => {
      logger.throttledError('ping-command', 'The /ping command failed.', error);
    });
  });

  try {
    const todaysNotes = await dailyNotes.initialize();

    client.once(Events.ClientReady, (readyClient) => {
      console.log('🌴 Paradise is awake.');

      void presence.start(todaysNotes).catch((error) => {
        logger.error('Could not start the presence rotation.', error);
      });

      dailyNotes.start((newNotes) => presence.refreshDailyNotes(newNotes));

      if (readyClient.user.id !== config.clientId) {
        logger.error('CLIENT_ID does not match the connected bot; /ping was not registered.');
        return;
      }

      void registerCommands({
        token: config.discordToken,
        clientId: config.clientId,
        guildId: config.guildId,
      }).catch((error) => {
        logger.error('Could not register /ping in the configured server.', error);
      });
    });

    await client.login(config.discordToken);
  } catch (error) {
    logger.error('Paradise could not connect to Discord.', error);
    await shutdown('startup failure', 1);
  }
}
