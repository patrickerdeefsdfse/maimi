// Native DSH skill provider. This module registers instructions, not a second
// chat backend, document store, model client, or medical/employment decision API.
import { readFileSync, lstatSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const name = 'megmeet-review-skills';
export const inject = ['skills'];
export const REVIEWED_RUNTIME = '0.2.0-rc.2';
export const PROVIDER = 'megmeet-review';
const ROOT = path.dirname(fileURLToPath(import.meta.url));
const MAX_SKILL_BYTES = 128 * 1024;
// DSH 0.2.0-rc.2's public BUNDLED_SKILL_RANK. No core import is needed, so this
// first-party plugin is also loadable as an absolute file URL in a frozen image.
const BUNDLED_RANK = 600;
export const CATALOG = Object.freeze([
  Object.freeze({ name: 'patent-review',
    description: '专利审核：核对技术交底、权利要求和说明书，定位有依据的问题与修改建议；检索材料不足时明确范围，不保证授权或法律结论。' }),
  Object.freeze({ name: 'onboarding-health-review',
    description: '入职体检报告辅助复核：按原报告整理异常、缺项和建议复核事项，避免对轻微偏离过度升级；不诊断、不作录用或淘汰决定。' }),
]);

function fail(code) { throw Object.assign(new Error(code), { code }); }
function sourceFile(root, skillName) {
  if (!CATALOG.some(row => row.name === skillName)) fail('REVIEW_SKILL_UNKNOWN');
  const directory = path.join(root, 'skills', skillName);
  for (const target of [path.join(root, 'skills'), directory]) {
    const info = lstatSync(target);
    if (!info.isDirectory() || info.isSymbolicLink()) fail('REVIEW_SKILL_DIRECTORY_INVALID');
  }
  const filename = path.join(directory, 'SKILL.md');
  const info = lstatSync(filename);
  if (!info.isFile() || info.isSymbolicLink() || info.size < 1 || info.size > MAX_SKILL_BYTES)
    fail('REVIEW_SKILL_FILE_INVALID');
  return { directory, filename };
}

export function skillBody(raw, expectedName) {
  if (typeof raw !== 'string' || Buffer.byteLength(raw) > MAX_SKILL_BYTES || raw.includes('\u0000'))
    fail('REVIEW_SKILL_CONTENT_INVALID');
  const match = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/u.exec(raw.replace(/^\uFEFF/, ''));
  if (!match) fail('REVIEW_SKILL_FRONTMATTER_REQUIRED');
  // Metadata displayed by the host is this reviewed CATALOG. Only a canonical
  // ASCII name is consumed from frontmatter; document YAML is never executed.
  const names = [...match[1].matchAll(/^name:[ \t]*([a-z0-9-]+)[ \t]*\r?$/gm)];
  if (names.length !== 1 || names[0][1] !== expectedName) fail('REVIEW_SKILL_NAME_MISMATCH');
  const content = raw.replace(/^\uFEFF/, '').slice(match[0].length).trim();
  if (!content) fail('REVIEW_SKILL_CONTENT_EMPTY');
  return content;
}

function snapshot(root) {
  return CATALOG.map(row => {
    const { directory, filename } = sourceFile(root, row.name);
    const content = skillBody(readFileSync(filename, 'utf8'), row.name);
    return { ...row, content, path: filename,
      invocation: { modelInvocable: true, userInvocable: true },
      source: 'bundled', provider: PROVIDER,
      resourceBase: { kind: 'directory', path: directory } };
  });
}

export function apply(ctx, config = {}) {
  if (!config || typeof config !== 'object' || Array.isArray(config) || Object.keys(config).length)
    fail('REVIEW_SKILL_CONFIGURATION_UNSUPPORTED');
  if (!ctx.skills || typeof ctx.skills.registerProvider !== 'function')
    fail('REVIEW_SKILL_REGISTRY_REQUIRED');
  // Validate both complete skill files before publishing either catalog entry.
  const definitions = snapshot(ROOT);
  ctx.skills.registerProvider(control => {
    const candidates = definitions.map(({ content: _content, ...definition }) => ({
      ...definition, rank: BUNDLED_RANK, locator: definition.name,
    }));
    return {
      name: PROVIDER,
      async list(options = {}) {
        control.signal.throwIfAborted(); options.signal?.throwIfAborted();
        return candidates.map(candidate => ({ ...candidate,
          invocation: { ...candidate.invocation }, resourceBase: { ...candidate.resourceBase } }));
      },
      async get(candidate, options = {}) {
        control.signal.throwIfAborted(); options.signal?.throwIfAborted();
        const definition = definitions.find(row => row.name === candidate.locator && row.name === candidate.name);
        if (!definition) fail('REVIEW_SKILL_UNKNOWN');
        // Changing a packaged skill while the host is live requires a reload;
        // don't serve a catalog from one revision and instructions from another.
        const { filename } = sourceFile(ROOT, definition.name);
        const content = skillBody(await readFile(filename, { encoding: 'utf8', signal: options.signal }), definition.name);
        control.signal.throwIfAborted(); options.signal?.throwIfAborted();
        if (content !== definition.content) fail('REVIEW_SKILL_RELOAD_REQUIRED');
        return { ...definition, invocation: { ...definition.invocation },
          resourceBase: { ...definition.resourceBase } };
      },
    };
  });
}
