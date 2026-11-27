import fs from 'node:fs';
import path from 'node:path';
import {writeDesktopRuntime, verifyDesktopRuntime} from '../shell/src/runtime-tree.ts';
import {DESKTOP_HOST_PROTOCOL_VERSION} from '../shell/src/host-protocol.ts';
const root = process.argv[2];
const bundled = JSON.parse(fs.readFileSync(path.join(root,'../../runtime/versions.json')));
const release = {schemaVersion:1, version:'0.2.2-rc.1', hostProtocolVersion:DESKTOP_HOST_PROTOCOL_VERSION,
  nodeVersion:bundled.node, pnpmVersion:bundled.pnpm};
const shared = ['@deepseek-ai/dsh','@deepseek-ai/dsh-desktop-host'];
writeDesktopRuntime(root, release, shared);
const actual = await verifyDesktopRuntime(root, release.version, {platform:'win32',arch:'x64'});
console.log(JSON.stringify({version:release.version, files:actual.files.length, verified:true}));
