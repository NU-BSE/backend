/**
 * `node:module`, reduced to the one thing bundled code asks it for.
 *
 * `createRequire(import.meta.url)` is the standard way an ESM file reaches a
 * CommonJS dependency. After bundling there are no modules left to resolve —
 * esbuild inlined every one — so any surviving call is either dead code behind
 * a branch that is not taken, or a genuine runtime resolution this environment
 * cannot do.
 *
 * Returning a `require` that throws distinguishes those two at exactly the
 * right moment: the common case (dead code) works, and the real case fails
 * with a message naming the module it wanted instead of a bare
 * "require is not defined".
 */

export function createRequire() {
  const require = (specifier) => {
    /*
     * Reading its own package.json for a version string is what a bundled
     * server almost always wants `require` for — the official
     * sequential-thinking server refuses to start without it, walking up from
     * import.meta.url looking for the file. There is no file, so the bundler
     * defines the two fields that matter and they are served from here.
     *
     * Matched by filename rather than by resolving the path: the server tries
     * several candidate directories, all of which are fictional in a bundle,
     * and each of them means the same request.
     */
    if (String(specifier).endsWith('package.json')) {
      const injected = typeof __CREEPY_PACKAGE__ !== 'undefined' ? __CREEPY_PACKAGE__ : null;
      if (injected) return injected;
    }
    throw new Error(
      `This MCP server tried to require("${specifier}") at runtime. ` +
        'Bundled servers cannot resolve modules dynamically.',
    );
  };
  require.resolve = (specifier) => specifier;
  require.cache = {};
  return require;
}

export const builtinModules = [];
export default { createRequire, builtinModules };
