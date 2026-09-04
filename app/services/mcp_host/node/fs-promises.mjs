/**
 * `fs/promises`, over the same in-memory store as `fs`.
 *
 * A separate module because it is a separate specifier: aliasing `fs` to a
 * *file* makes esbuild try to resolve `fs/promises` as a path inside it, which
 * fails with "not a directory" — an error that names the shim rather than the
 * import, and reads like the sandbox is broken rather than incomplete.
 *
 * The official filesystem server imports only this form.
 */
import fs from './fs.mjs';

export const {
  readFile, writeFile, appendFile, mkdir, readdir, unlink, rm, stat, rename,
  copyFile, access,
} = fs.promises;

export default fs.promises;
