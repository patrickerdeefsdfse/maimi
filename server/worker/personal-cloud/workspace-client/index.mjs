// Client discovery only: never activate the upstream filesystem/Remote Host.
// Retain the exact official rc5 browser bundle and original module identity.
// On future npm upgrades, reject a stale adapter instead of silently mixing versions.
import fs from 'node:fs';
import {createHash} from 'node:crypto';
const originalRoot='/opt/dsh/node_modules/@deepseek-ai/dsh-api-workspace-controller';
const original=JSON.parse(fs.readFileSync(originalRoot+'/package.json','utf8'));
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
if(original.version!=='0.2.2-rc.5'||hash(fs.readFileSync(originalRoot+'/lib/client.js'))!=='70b58daaf44c3c3e7cd0bf9674a1c8a68d612e53338f59eaa891c7bac588021b'||hash(fs.readFileSync(new URL('./client.js',import.meta.url)))!=='70b58daaf44c3c3e7cd0bf9674a1c8a68d612e53338f59eaa891c7bac588021b')
  throw Error('MAIMI_WORKSPACE_CLIENT_UPSTREAM_PIN_MISMATCH');
export function apply() {}
