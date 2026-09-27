import test from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig } from '../src/config.js';

test('loadConfig requires all Discord settings', () => {
  assert.throws(
    () => loadConfig({}),
    /DISCORD_TOKEN, CLIENT_ID, GUILD_ID/,
  );
});

test('loadConfig accepts Discord IDs without exposing the token', () => {
  const config = loadConfig({
    DISCORD_TOKEN: 'secret-token',
    CLIENT_ID: '123456789012345678',
    GUILD_ID: '987654321098765432',
  });

  assert.deepEqual(config, {
    discordToken: 'secret-token',
    clientId: '123456789012345678',
    guildId: '987654321098765432',
  });
  assert.equal(Object.isFrozen(config), true);
});
