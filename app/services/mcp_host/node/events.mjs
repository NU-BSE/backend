/**
 * A minimal EventEmitter.
 *
 * Pure bookkeeping over a map of arrays — nothing here needs an event loop or
 * a host. Servers reach for it constantly, and refusing one over it would be
 * refusing over a data structure.
 */

export class EventEmitter {
  constructor() {
    this._events = new Map();
    this._maxListeners = 10;
  }

  on(event, listener) {
    const listeners = this._events.get(event) ?? [];
    listeners.push(listener);
    this._events.set(event, listeners);
    return this;
  }

  addListener(event, listener) {
    return this.on(event, listener);
  }

  once(event, listener) {
    const wrapper = (...args) => {
      this.off(event, wrapper);
      listener(...args);
    };
    wrapper.listener = listener;
    return this.on(event, wrapper);
  }

  off(event, listener) {
    const listeners = this._events.get(event);
    if (!listeners) return this;
    const next = listeners.filter(
      (candidate) => candidate !== listener && candidate.listener !== listener,
    );
    if (next.length) this._events.set(event, next);
    else this._events.delete(event);
    return this;
  }

  removeListener(event, listener) {
    return this.off(event, listener);
  }

  removeAllListeners(event) {
    if (event === undefined) this._events.clear();
    else this._events.delete(event);
    return this;
  }

  emit(event, ...args) {
    const listeners = this._events.get(event);
    if (!listeners || listeners.length === 0) {
      /*
       * Node throws when an 'error' event has no listener, and code relies on
       * that to surface failures rather than swallow them. Keeping the
       * behaviour means a server misbehaves here the same way it would on a
       * desktop, which is the point of a shim.
       */
      if (event === 'error') throw args[0] ?? new Error('Unhandled error event');
      return false;
    }
    for (const listener of [...listeners]) listener(...args);
    return true;
  }

  listenerCount(event) {
    return (this._events.get(event) ?? []).length;
  }

  listeners(event) {
    return [...(this._events.get(event) ?? [])];
  }

  eventNames() {
    return [...this._events.keys()];
  }

  setMaxListeners(count) {
    this._maxListeners = count;
    return this;
  }

  getMaxListeners() {
    return this._maxListeners;
  }
}

// Node exposes the class both as the default and as a named export, and
// bundled code uses whichever its author preferred.
export default EventEmitter;
