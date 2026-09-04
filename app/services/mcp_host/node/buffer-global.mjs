/**
 * `Buffer` as a global.
 *
 * Node exposes Buffer without an import, so servers write `Buffer.from(...)`
 * with nothing at the top of the file — exactly the trap `process` was. An
 * alias rewrites import specifiers and reaches none of those uses; --inject
 * binds the name over the unbound global, which does.
 *
 * A separate file from `buffer.mjs` because --inject binds *every* export as a
 * global, and `constants` becoming one would collide with anything else that
 * uses that name.
 */
export { Buffer } from './buffer.mjs';
