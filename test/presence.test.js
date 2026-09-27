import test from 'node:test';
import assert from 'node:assert/strict';
import { activities } from '../src/presence/activities.js';
import { PresenceManager } from '../src/presence/presenceManager.js';

function key(presence) {
  const activity = presence.activities[0];
  return `${activity.type}:${activity.state ?? activity.name}`;
}

test('the activity pool has at least sixty entries across varied categories', () => {
  const categoryNames = Object.keys(activities);
  const activityCount = Object.values(activities).flat().length;

  assert.equal(categoryNames.length, 8);
  assert.ok(activityCount >= 60);
});

test('presence changes do not immediately repeat', async () => {
  const updates = [];
  const client = {
    user: {
      setPresence(presence) {
        updates.push(presence);
      },
    },
  };
  const manager = new PresenceManager(client, {
    random: () => 0.5,
    minimumInterval: 60_000,
    maximumInterval: 60_000,
  });

  await manager.start(['A quiet daily note.']);
  await manager.rotate();
  await manager.rotate();
  manager.stop();

  assert.equal(updates.length, 3);
  assert.notEqual(key(updates[0]), key(updates[1]));
  assert.notEqual(key(updates[1]), key(updates[2]));
});

test('reapplying a one-note daily set falls back to another activity', async () => {
  const updates = [];
  const client = {
    user: {
      setPresence(presence) {
        updates.push(presence);
      },
    },
  };
  const manager = new PresenceManager(client, {
    random: () => 0,
    minimumInterval: 60_000,
    maximumInterval: 60_000,
  });

  await manager.start(['Still here.']);
  await manager.refreshDailyNotes(['Still here.']);
  manager.stop();

  assert.equal(updates.length, 2);
  assert.notEqual(key(updates[0]), key(updates[1]));
});
