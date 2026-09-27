import { SlashCommandBuilder } from 'discord.js';

export const pingCommand = new SlashCommandBuilder()
  .setName('ping')
  .setDescription('Checks whether Paradise is still here.');

export async function handlePing(interaction) {
  await interaction.reply('alive unfortunately.');
}
