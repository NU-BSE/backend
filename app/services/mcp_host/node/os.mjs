/**
 * `node:os`, answering for a phone.
 *
 * Servers use this almost exclusively for `homedir()`, to place a dot-directory
 * for their state. The virtual filesystem is where that goes, so this returns
 * a stable root inside it rather than pretending to know anything about the
 * host — which the sandbox deliberately cannot see.
 */

export function homedir() {
  return '/home/mcp';
}

export function tmpdir() {
  return '/tmp';
}

export function platform() {
  return 'android';
}

export function type() {
  return 'Linux';
}

export function arch() {
  return 'arm64';
}

export function hostname() {
  return 'creepy';
}

export function cpus() {
  return [];
}

export function totalmem() {
  return 0;
}

export function freemem() {
  return 0;
}

export const EOL = '\n';

export default { homedir, tmpdir, platform, type, arch, hostname, cpus, totalmem, freemem, EOL };
