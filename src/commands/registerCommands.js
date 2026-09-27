import { REST, Routes } from 'discord.js';
import { pingCommand } from './ping.js';

export async function registerCommands({ token, clientId, guildId }) {
  const rest = new REST({ version: '10' }).setToken(token);

  await rest.put(
    Routes.applicationGuildCommands(clientId, guildId),
    { body: [pingCommand.toJSON()] },
  );
}
