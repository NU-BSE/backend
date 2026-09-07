/**
 * Stands in for the MCP stdio transport inside a bundled server.
 *
 * An MCP server written for Node ends its entry file with roughly:
 *
 *     await server.connect(new StdioServerTransport());
 *
 * On a phone there is no stdin, no stdout and no process to own them. Rather
 * than ask every server to be rewritten, the bundler *aliases* the SDK's stdio
 * module to this file, so the server's own source is untouched and the object
 * it constructs simply talks to the host application instead of to file
 * descriptors.
 *
 * The host is whatever evaluates the bundle. It provides
 * `globalThis.__creepyMcpHost` with:
 *
 *   receive(message)  — called by this transport for every outgoing message
 *   ready(transport)  — called once, handing back a `deliver(message)` hook
 *
 * Both directions carry parsed JSON-RPC objects. There is no framing, because
 * there is no stream to frame: newline-delimited JSON exists to find message
 * boundaries in a byte stream, and passing objects across a function call has
 * boundaries already.
 */

class HostTransport {
  constructor() {
    this.onmessage = undefined;
    this.onclose = undefined;
    this.onerror = undefined;
    this._started = false;
    this._closed = false;
  }

  async start() {
    if (this._started) {
      // The SDK calls start() itself from connect(); a server that also calls
      // it explicitly must not end up with two registrations.
      return;
    }
    this._started = true;

    const host = globalThis.__creepyMcpHost;
    if (!host || typeof host.ready !== 'function') {
      throw new Error(
        'No MCP host is present. This bundle must be evaluated by the Creepy.IM ' +
          'runtime, which installs globalThis.__creepyMcpHost before loading it.',
      );
    }

    host.ready({
      // The host calls this for every message it wants the server to see.
      deliver: (message) => {
        if (this._closed) return;
        try {
          this.onmessage?.(message);
        } catch (error) {
          this.onerror?.(error instanceof Error ? error : new Error(String(error)));
        }
      },
      close: () => this.close(),
    });
  }

  async send(message) {
    if (this._closed) return;
    const host = globalThis.__creepyMcpHost;
    host?.receive?.(message);
  }

  async close() {
    if (this._closed) return;
    this._closed = true;
    this.onclose?.();
  }
}

export { HostTransport as StdioServerTransport };
export { HostTransport as StdioClientTransport };
export default HostTransport;
