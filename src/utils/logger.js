function describeError(error) {
  if (error instanceof Error) {
    return error.stack ?? `${error.name}: ${error.message}`;
  }

  return String(error);
}

export function createLogger({ secrets = [], throttleMs = 60_000 } = {}) {
  const hiddenValues = secrets.filter(Boolean);
  const lastLoggedAt = new Map();

  function sanitize(value) {
    return hiddenValues.reduce(
      (text, secret) => text.replaceAll(secret, '[redacted]'),
      String(value),
    );
  }

  function write(level, message, error) {
    const parts = [`[Paradise] ${sanitize(message)}`];
    if (error !== undefined) {
      parts.push(sanitize(describeError(error)));
    }
    console[level](...parts);
  }

  return {
    info(message) {
      write('log', message);
    },
    warn(message, error) {
      write('warn', message, error);
    },
    error(message, error) {
      write('error', message, error);
    },
    throttledWarn(key, message, error) {
      const now = Date.now();
      const last = lastLoggedAt.get(key) ?? 0;
      if (now - last < throttleMs) {
        return;
      }
      lastLoggedAt.set(key, now);
      write('warn', message, error);
    },
    throttledError(key, message, error) {
      const now = Date.now();
      const last = lastLoggedAt.get(key) ?? 0;
      if (now - last < throttleMs) {
        return;
      }
      lastLoggedAt.set(key, now);
      write('error', message, error);
    },
  };
}
