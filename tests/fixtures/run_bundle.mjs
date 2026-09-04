// Runs a translated bundle the way the device does: as an async function body,
// with a host bridge installed. Used by the bundler tests so the shims are
// exercised rather than merely present in the output.
import { readFileSync } from 'node:fs';

const code = readFileSync(process.argv[2], 'utf8');
const seeded = process.argv[3] ? JSON.parse(process.argv[3]) : {};

let saved = null;
globalThis.__CREEPY_FILES__ = seeded;
globalThis.__creepyMcpHost = {
  env: {},
  log: () => {},
  ready: () => {},
  receive: () => {},
  saveFiles: (files) => {
    saved = files;
  },
};

await new Function(`return (async () => {\n${code}\n})()`)();

console.log(JSON.stringify({ result: globalThis.__result ?? null, saved }));
