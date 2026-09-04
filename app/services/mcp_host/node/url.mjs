/**
 * The parts of `node:url` a server can meaningfully use here.
 *
 * `URL` and `URLSearchParams` are already global in a browser context, so they
 * are re-exported rather than reimplemented.
 *
 * `fileURLToPath` is the interesting one: it is overwhelmingly used as
 * `fileURLToPath(import.meta.url)` to find the module's own directory, usually
 * to locate a sibling data file. There are no sibling files in a bundle — it
 * is one file — so this converts the URL honestly and lets any subsequent
 * filesystem call be the thing that fails, rather than failing here where the
 * cause would be less obvious.
 */

export function fileURLToPath(input) {
  const url = typeof input === 'string' ? new URL(input) : input;
  if (url.protocol !== 'file:') {
    throw new TypeError('The URL must be of scheme file');
  }
  return decodeURIComponent(url.pathname);
}

export function pathToFileURL(path) {
  return new URL(`file://${encodeURI(String(path)).replace(/#/gu, '%23')}`);
}

export function parse(input) {
  const url = new URL(input, 'file:///');
  return {
    protocol: url.protocol,
    host: url.host,
    hostname: url.hostname,
    port: url.port,
    pathname: url.pathname,
    search: url.search,
    hash: url.hash,
    href: url.href,
    query: url.search.startsWith('?') ? url.search.slice(1) : url.search,
  };
}

export function format(url) {
  return typeof url === 'string' ? url : String(url.href ?? '');
}

const URLCtor = globalThis.URL;
const URLSearchParamsCtor = globalThis.URLSearchParams;
export { URLCtor as URL, URLSearchParamsCtor as URLSearchParams };

export default {
  fileURLToPath,
  pathToFileURL,
  parse,
  format,
  URL: URLCtor,
  URLSearchParams: URLSearchParamsCtor,
};
