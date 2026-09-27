export function randomInteger(minimum, maximum, random = Math.random) {
  if (!Number.isInteger(minimum) || !Number.isInteger(maximum) || maximum < minimum) {
    throw new RangeError('Invalid random integer range.');
  }

  return Math.floor(random() * (maximum - minimum + 1)) + minimum;
}

export function pickRandom(items, random = Math.random) {
  if (!Array.isArray(items) || items.length === 0) {
    return undefined;
  }

  return items[randomInteger(0, items.length - 1, random)];
}

export function pickDifferent(items, previous, key = (item) => item, random = Math.random) {
  const choices = items.filter((item) => key(item) !== key(previous));
  return pickRandom(choices.length > 0 ? choices : items, random);
}

export function weightedPick(entries, random = Math.random) {
  const validEntries = entries.filter(({ weight }) => Number.isFinite(weight) && weight > 0);
  const totalWeight = validEntries.reduce((sum, { weight }) => sum + weight, 0);

  if (totalWeight === 0) {
    return undefined;
  }

  let target = random() * totalWeight;

  for (const entry of validEntries) {
    target -= entry.weight;
    if (target < 0) {
      return entry.value;
    }
  }

  return validEntries.at(-1).value;
}

export function sampleWithoutReplacement(items, count, random = Math.random) {
  const pool = [...items];
  const result = [];
  const amount = Math.min(Math.max(0, count), pool.length);

  for (let index = 0; index < amount; index += 1) {
    const selectedIndex = randomInteger(0, pool.length - 1, random);
    result.push(pool.splice(selectedIndex, 1)[0]);
  }

  return result;
}
