import { ActivityType } from 'discord.js';
import { activities, activityCategories } from './activities.js';
import { pickDifferent, randomInteger, weightedPick } from '../utils/random.js';

export const MIN_ACTIVITY_INTERVAL_MS = 20 * 60 * 1_000;
export const MAX_ACTIVITY_INTERVAL_MS = 60 * 60 * 1_000;

function activityKey(activity) {
  if (!activity) {
    return undefined;
  }
  return `${activity.type}:${activity.state ?? activity.name}`;
}

function noteActivity(note) {
  return {
    name: 'Custom Status',
    state: note,
    type: ActivityType.Custom,
  };
}

export class PresenceManager {
  constructor(client, {
    random = Math.random,
    logger = console,
    minimumInterval = MIN_ACTIVITY_INTERVAL_MS,
    maximumInterval = MAX_ACTIVITY_INTERVAL_MS,
  } = {}) {
    this.client = client;
    this.random = random;
    this.logger = logger;
    this.minimumInterval = minimumInterval;
    this.maximumInterval = maximumInterval;
    this.dailyNotes = [];
    this.lastActivity = undefined;
    this.lastCategory = undefined;
    this.timer = undefined;
    this.running = false;
  }

  async start(dailyNotes = []) {
    if (this.running) {
      return;
    }

    this.running = true;
    this.dailyNotes = [...dailyNotes];
    const initialActivity = this.#pickDailyNote() ?? this.#pickRegularActivity().activity;
    await this.#apply(initialActivity);
    this.#scheduleNext();
  }

  stop() {
    this.running = false;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = undefined;
    }
  }

  async refreshDailyNotes(dailyNotes) {
    this.dailyNotes = [...dailyNotes];
    if (!this.running) {
      return;
    }

    const activity = this.#pickDailyNote() ?? this.#pickRegularActivity().activity;
    await this.#apply(activity);
    this.#scheduleNext();
  }

  async rotate() {
    if (!this.running) {
      return;
    }

    try {
      if (this.random() >= 0.11) {
        const useDailyNote = this.dailyNotes.length > 0 && this.random() < 0.16;
        const selected = useDailyNote ? this.#pickDailyNote() : undefined;
        const activity = selected ?? this.#pickRegularActivity().activity;
        await this.#apply(activity);
      }
    } finally {
      this.#scheduleNext();
    }
  }

  #pickDailyNote() {
    const noteActivities = this.dailyNotes.map(noteActivity);
    const selected = pickDifferent(noteActivities, this.lastActivity, activityKey, this.random);

    return activityKey(selected) === activityKey(this.lastActivity) ? undefined : selected;
  }

  #pickRegularActivity() {
    let categories = activityCategories;

    if (this.lastCategory && this.random() < 0.62) {
      categories = activityCategories.filter(({ name }) => name !== this.lastCategory);
    }

    const category = weightedPick(
      categories.map(({ name, weight }) => ({ value: name, weight })),
      this.random,
    );
    const activity = pickDifferent(
      activities[category],
      this.lastActivity,
      activityKey,
      this.random,
    );

    this.lastCategory = category;
    return { activity, category };
  }

  async #apply(activity) {
    if (!activity || !this.client.user) {
      return;
    }

    try {
      await this.client.user.setPresence({
        activities: [activity],
        status: 'online',
      });
      this.lastActivity = activity;
    } catch (error) {
      this.logger.error('Could not update the Discord presence.', error);
    }
  }

  #scheduleNext() {
    if (!this.running) {
      return;
    }

    if (this.timer) {
      clearTimeout(this.timer);
    }

    const delay = randomInteger(this.minimumInterval, this.maximumInterval, this.random);
    this.timer = setTimeout(() => {
      this.timer = undefined;
      void this.rotate().catch((error) => {
        this.logger.error('Presence rotation failed.', error);
      });
    }, delay);

    this.timer.unref?.();
  }
}
