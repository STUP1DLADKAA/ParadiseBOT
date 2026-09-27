import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { DailyNotes, selectDailyNotes, utcDateKey } from '../src/personality/dailyNotes.js';

const quietLogger = {
  error() {},
  warn() {},
};

test('utcDateKey uses UTC dates', () => {
  assert.equal(utcDateKey(new Date('2026-03-02T23:59:59Z')), '2026-03-02');
});

test('selectDailyNotes avoids notes from the previous day when possible', () => {
  const selected = selectDailyNotes(['old', 'new', 'other'], ['old'], () => 0);
  assert.deepEqual(selected, ['new']);
});

test('daily notes survive a restart and change on a new day', async (context) => {
  const directory = await mkdtemp(join(tmpdir(), 'paradise-notes-'));
  context.after(() => rm(directory, { recursive: true, force: true }));
  const stateFile = join(directory, 'daily-notes.json');
  const pool = ['one', 'two', 'three', 'four', 'five'];

  const firstRun = new DailyNotes({
    stateFile,
    pool,
    random: () => 0,
    now: () => new Date('2026-09-27T10:00:00Z'),
    logger: quietLogger,
  });
  const firstNotes = await firstRun.initialize();

  const restarted = new DailyNotes({
    stateFile,
    pool,
    random: () => 0.99,
    now: () => new Date('2026-09-27T20:00:00Z'),
    logger: quietLogger,
  });
  const restartedNotes = await restarted.initialize();

  assert.deepEqual(restartedNotes, firstNotes);

  const nextDay = new DailyNotes({
    stateFile,
    pool,
    random: () => 0,
    now: () => new Date('2026-09-28T00:01:00Z'),
    logger: quietLogger,
  });
  const nextNotes = await nextDay.initialize();

  assert.notDeepEqual(nextNotes, firstNotes);
  const saved = JSON.parse(await readFile(stateFile, 'utf8'));
  assert.equal(saved.date, '2026-09-28');
  assert.deepEqual(saved.notes, nextNotes);
});
