'use strict';

// Diagnostic output must never determine whether a chat or Host can close.
// A stream may fail asynchronously after write() (e.g. a detached terminal).
// Keep one error listener per sink; do not suppress application exceptions.
const loggers = new WeakMap();
const silent = Object.freeze({ write: () => false });
function diagnosticLog(sink) {
  if (!sink || typeof sink.write !== 'function') return silent;
  const cached = loggers.get(sink);
  if (cached) return cached;
  let failed = false;
  const fail = () => { failed = true; };
  sink.on?.('error', fail);
  sink.once?.('close', fail);
  const logger = Object.freeze({
    write(text) {
      if (failed || sink.destroyed || sink.writableEnded || sink.writable === false) return false;
      try {
        return sink.write(text, error => { if (error) fail(); }) !== false;
      } catch {
        fail();
        return false;
      }
    },
  });
  loggers.set(sink, logger);
  loggers.set(logger, logger);
  return logger;
}
module.exports = { diagnosticLog };
