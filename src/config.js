const REQUIRED_VARIABLES = ['DISCORD_TOKEN', 'CLIENT_ID', 'GUILD_ID'];
const SNOWFLAKE_PATTERN = /^\d{16,22}$/;

export function loadConfig(environment = process.env) {
  const missing = REQUIRED_VARIABLES.filter((name) => !environment[name]?.trim());

  if (missing.length > 0) {
    throw new Error(`Missing required environment variables: ${missing.join(', ')}`);
  }

  const clientId = environment.CLIENT_ID.trim();
  const guildId = environment.GUILD_ID.trim();

  if (!SNOWFLAKE_PATTERN.test(clientId)) {
    throw new Error('CLIENT_ID must be a valid Discord ID.');
  }

  if (!SNOWFLAKE_PATTERN.test(guildId)) {
    throw new Error('GUILD_ID must be a valid Discord ID.');
  }

  return Object.freeze({
    discordToken: environment.DISCORD_TOKEN.trim(),
    clientId,
    guildId,
  });
}
