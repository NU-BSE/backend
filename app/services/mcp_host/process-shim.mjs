/**
 * A `process` for a server that is no longer a process.
 *
 * Almost every Node MCP server reads `process.env` for its configuration, and
 * many write diagnostics to `process.stderr`. Neither needs a real process,
 * so both are provided rather than made to fail — refusing a server because it
 * logs would refuse most of them.
 *
 * `env` is populated by the host from the values the user supplied, so the
 * server's own `process.env.GITHUB_TOKEN` lookup works unchanged.
 *
 * What is deliberately absent: `exit`, `kill`, `cwd` as anything but a stub,
 * and the real argv. A server that tries to exit gets a thrown error the host
 * can report, which is far better than a silent no-op that leaves it wedged.
 *
 * This is injected, not aliased. `process` is a *global* in Node — servers
 * write `process.env.TOKEN` without importing anything — so rewriting import
 * specifiers reaches none of them. esbuild's --inject binds the exported
 * `process` name over the unbound global instead, which is what actually
 * substitutes it. Getting this wrong is silent: the bundle builds, and every
 * server then reads an empty environment.
 */

const listeners = new Map();

function write(stream) {
  return (chunk) => {
    globalThis.__creepyMcpHost?.log?.(stream, String(chunk));
    return true;
  };
}

const process = {
  env: new Proxy(
    {},
    {
      get: (_target, name) => globalThis.__creepyMcpHost?.env?.[name],
      has: (_target, name) => name in (globalThis.__creepyMcpHost?.env ?? {}),
      ownKeys: () => Reflect.ownKeys(globalThis.__creepyMcpHost?.env ?? {}),
      getOwnPropertyDescriptor: () => ({ enumerable: true, configurable: true }),
    },
  ),
  argv: ['node', 'mcp-server'],
  platform: 'android',
  version: 'v22.0.0',
  versions: { node: '22.0.0' },
  stdout: { write: write('stdout'), isTTY: false },
  stderr: { write: write('stderr'), isTTY: false },
  exit(code) {
    throw new Error(`The MCP server called process.exit(${code ?? 0}).`);
  },
  cwd: () => '/',
  nextTick: (fn, ...args) => {
    Promise.resolve().then(() => fn(...args));
  },
  on(event, handler) {
    listeners.set(event, [...(listeners.get(event) ?? []), handler]);
    return process;
  },
  off: () => process,
  removeListener: () => process,
  emitWarning: () => {},
  hrtime: Object.assign(() => [0, 0], {
    bigint: () => BigInt(Math.round(performance.now() * 1e6)),
  }),
};

// Named `process` because --inject binds exports by name onto the global.
export { process };
export default process;
