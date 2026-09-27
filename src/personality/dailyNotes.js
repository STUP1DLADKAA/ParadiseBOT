import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { notes as defaultNotes } from './notes.js';
import { sampleWithoutReplacement, weightedPick } from '../utils/random.js';

const NOTE_COUNTS = Object.freeze([
  { value: 1, weight: 18 },
  { value: 2, weight: 44 },
  { value: 3, weight: 30 },
  { value: 4, weight: 8 },
]);

export function utcDateKey(date = new Date()) {
  return date.toISOString().slice(0, 10);
}

function sameSet(first, second) {
  if (first.length !== second.length) {
    return false;
  }

  const firstSorted = [...first].sort();
  const secondSorted = [...second].sort();
  return firstSorted.every((note, index) => note === secondSorted[index]);
}

export function selectDailyNotes(pool, previousNotes = [], random = Math.random) {
  const count = weightedPick(NOTE_COUNTS, random);
  const freshPool = pool.filter((note) => !previousNotes.includes(note));
  const candidates = freshPool.length >= count ? freshPool : pool;

  for (let attempt = 0; attempt < 5; attempt += 1) {
    const selection = sampleWithoutReplacement(candidates, count, random);
    if (!sameSet(selection, previousNotes)) {
      return selection;
    }
  }

  const fallback = pool.find((note) => !previousNotes.includes(note));
  return fallback ? [fallback] : sampleWithoutReplacement(pool, count, random);
}

function isStoredState(value) {
  return Boolean(
    value
      && typeof value.date === 'string'
      && Array.isArray(value.notes)
      && value.notes.length > 0
      && value.notes.every((note) => typeof note === 'string' && note.length > 0),
  );
}

export class DailyNotes {
  constructor({
    stateFile = 'data/daily-notes.json',
    pool = defaultNotes,
    random = Math.random,
    now = () => new Date(),
    logger = console,
  } = {}) {
    this.stateFile = stateFile;
    this.pool = pool;
    this.random = random;
    this.now = now;
    this.logger = logger;
    this.currentDate = undefined;
    this.currentNotes = [];
    this.timer = undefined;
    this.onChange = undefined;
  }

  async initialize() {
    const { notes } = await this.#refresh();
    return [...notes];
  }

  start(onChange) {
    this.onChange = onChange;
    this.#scheduleRollover();
  }

  stop() {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = undefined;
    }
  }

  getNotes() {
    return [...this.currentNotes];
  }

  async #refresh() {
    const today = utcDateKey(this.now());

    if (this.currentDate === today && this.currentNotes.length > 0) {
      return { notes: this.currentNotes, changed: false };
    }

    const stored = await this.#readState();
    if (stored?.date === today) {
      this.currentDate = today;
      this.currentNotes = [...stored.notes];
      return { notes: this.currentNotes, changed: false };
    }

    const previousNotes = this.currentNotes.length > 0
      ? this.currentNotes
      : (stored?.notes ?? []);
    const selected = selectDailyNotes(this.pool, previousNotes, this.random);

    this.currentDate = today;
    this.currentNotes = selected;

    try {
      await this.#writeState({
        version: 1,
        date: today,
        notes: selected,
        previousNotes,
      });
    } catch (error) {
      this.logger.error('Could not persist today\'s notes.', error);
    }

    return { notes: selected, changed: true };
  }

  async #readState() {
    try {
      const contents = await readFile(this.stateFile, 'utf8');
      const parsed = JSON.parse(contents);

      if (!isStoredState(parsed)) {
        this.logger.warn('Saved daily notes were invalid; choosing a new set.');
        return undefined;
      }

      return parsed;
    } catch (error) {
      if (error?.code !== 'ENOENT') {
        this.logger.warn('Could not read saved daily notes; choosing a new set.', error);
      }
      return undefined;
    }
  }

  async #writeState(state) {
    const temporaryFile = `${this.stateFile}.tmp`;
    await mkdir(dirname(this.stateFile), { recursive: true });
    await writeFile(temporaryFile, `${JSON.stringify(state, null, 2)}\n`, 'utf8');
    await rename(temporaryFile, this.stateFile);
  }

  #scheduleRollover() {
    this.stop();

    const now = this.now();
    const nextMidnight = Date.UTC(
      now.getUTCFullYear(),
      now.getUTCMonth(),
      now.getUTCDate() + 1,
      0,
      0,
      1,
    );
    const delay = Math.max(1_000, nextMidnight - now.getTime());

    this.timer = setTimeout(async () => {
      try {
        const result = await this.#refresh();
        if (result.changed && this.onChange) {
          await this.onChange([...result.notes]);
        }
      } catch (error) {
        this.logger.error('Could not refresh the daily notes.', error);
      } finally {
        this.#scheduleRollover();
      }
    }, delay);

    this.timer.unref?.();
  }
}
