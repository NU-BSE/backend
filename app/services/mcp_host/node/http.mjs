/**
 * `node:http`, which in the sandbox is `node:https` — see that file.
 *
 * The distinction is the scheme, and the scheme is carried in the URL rather
 * than by the module. Keeping them as separate files means a server importing
 * either gets the same behaviour instead of one of them being refused.
 *
 * Node's HTTP client, over the sandbox's fetch proxy.
 *
 * An MCP server exists to call an API, and a good share of them still do it
 * with `https.get` rather than `fetch`. Refusing those was refusing the
 * feature.
 *
 * Every request goes out through `fetch`, which in the sandbox is not the
 * platform's — it is the bridge to the host, so the app sees, and can refuse,
 * whatever a server talks to. That indirection is also what makes this
 * possible at all: a null-origin document cannot reach an ordinary API
 * directly, and there are no sockets here to implement Node's client on.
 *
 * The shape is deliberately narrow. `request` and `get` return something that
 * accepts `write`/`end` and emits `response`; the response emits `data` once
 * and then `end`. That covers how this API is actually used and nothing more:
 * there is no streaming, no keep-alive, no socket events. A server that needs
 * those will find them absent rather than subtly wrong.
 */

class Emitter {
  constructor() {
    this._handlers = new Map();
  }

  on(event, handler) {
    const handlers = this._handlers.get(event) ?? [];
    handlers.push(handler);
    this._handlers.set(event, handlers);
    return this;
  }

  once(event, handler) {
    return this.on(event, handler);
  }

  emit(event, ...args) {
    for (const handler of this._handlers.get(event) ?? []) handler(...args);
    return this;
  }

  setEncoding() {
    return this;
  }

  removeListener() {
    return this;
  }
}

function toUrl(input, options) {
  if (typeof input === 'string') return input;
  if (input && typeof input === 'object' && typeof input.href === 'string') return input.href;
  const source = input || options || {};
  const protocol = source.protocol || 'http:';
  const host = source.hostname || source.host || 'localhost';
  const port = source.port ? `:${source.port}` : '';
  const path = source.path || source.pathname || '/';
  return `${protocol}//${host}${port}${path}`;
}

export function request(input, optionsOrCallback, maybeCallback) {
  const options = typeof optionsOrCallback === 'object' ? optionsOrCallback : {};
  const callback =
    typeof optionsOrCallback === 'function' ? optionsOrCallback : maybeCallback;

  const url = toUrl(input, options);
  const method = (typeof input === 'object' && input?.method) || options.method || 'GET';
  const headers = (typeof input === 'object' && input?.headers) || options.headers || {};

  const outgoing = new Emitter();
  const chunks = [];

  outgoing.write = (chunk) => {
    chunks.push(typeof chunk === 'string' ? chunk : new TextDecoder().decode(chunk));
    return true;
  };
  outgoing.setHeader = (name, value) => {
    headers[name] = value;
  };
  outgoing.setTimeout = () => outgoing;
  outgoing.destroy = () => outgoing;
  outgoing.abort = () => outgoing;

  outgoing.end = (chunk) => {
    if (chunk) outgoing.write(chunk);
    const body = chunks.join('');

    fetch(url, {
      method,
      headers,
      ...(body && method !== 'GET' && method !== 'HEAD' ? { body } : {}),
    })
      .then(async (response) => {
        const text = await response.text();
        const incoming = new Emitter();
        incoming.statusCode = response.status;
        incoming.statusMessage = response.statusText;
        // The host returns headers as a plain object; Node exposes them the
        // same way, lower-cased.
        incoming.headers = response.headers?.raw ?? {};
        if (callback) callback(incoming);
        outgoing.emit('response', incoming);
        // One chunk, then end. Nothing here streams: the host has already read
        // the whole body by the time it crosses the bridge.
        incoming.emit('data', text);
        incoming.emit('end');
      })
      .catch((error) => outgoing.emit('error', error));

    return outgoing;
  };

  return outgoing;
}

export function get(input, optionsOrCallback, maybeCallback) {
  const outgoing = request(input, optionsOrCallback, maybeCallback);
  outgoing.end();
  return outgoing;
}

export const Agent = class Agent {};
export const globalAgent = new Agent();

export default { request, get, Agent, globalAgent };
