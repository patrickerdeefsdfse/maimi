'use strict';
// The pinned nsisunz extractor uses MAX_PATH. Derive the bound from every
// packaged file; never prune runtime files merely to fit an installer path.
const fs = require('node:fs'), path = require('node:path');
function measurePayload(root) {
  root = path.resolve(root);
  let fileCount = 0, longestRelativePath = '';
  function walk(directory, relative = '') {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name), rel = path.win32.join(relative, entry.name);
      if (entry.isSymbolicLink() || (!entry.isFile() && !entry.isDirectory())) throw Error('Unsupported packaged filesystem object: ' + rel);
      if (entry.name.length > 255) throw Error('Packaged component exceeds Windows limit: ' + rel);
      if (rel.length > longestRelativePath.length) longestRelativePath = rel;
      if (entry.isDirectory()) walk(absolute, rel); else fileCount++;
    }
  }
  walk(root);
  if (!fileCount) throw Error('Empty packaged payload');
  // Include generated uninstaller even for a tiny synthetic payload.
  const longestRelativeChars = Math.max(longestRelativePath.length, 64);
  const maxInstallDirectoryChars = 259 - 1 - longestRelativeChars;
  if (maxInstallDirectoryChars < 16) throw Error('Payload paths cannot fit a usable install directory');
  return { fileCount, longestRelativePath, longestRelativeChars, maxInstallDirectoryChars, maxFullPathChars: 259 };
}
function writeInstallerInclude(root, destination) {
  const policy = measurePayload(root), source = path.join(__dirname, 'installer.nsh');
  if (/[\r\n"$]/.test(source)) throw Error('Unsafe NSIS include path');
  fs.writeFileSync(destination, `!define MEGMEET_MAX_INSTALL_DIR ${policy.maxInstallDirectoryChars}\n!include "${source}"\n`, 'utf8');
  fs.writeFileSync(destination + '.json', JSON.stringify(policy, null, 2) + '\n');
  return { include: destination, ...policy };
}
module.exports = { measurePayload, writeInstallerInclude };
