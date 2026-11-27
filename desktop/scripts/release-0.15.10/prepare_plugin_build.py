"""Prepare the two upstream release checkouts in a private, standalone build tree."""
import argparse
import hashlib
import json
from pathlib import Path


MEMORY_STORAGE = """
const __maimiMemoryValues = new Map();
const __maimiMemoryStorage = {
  getItem(key) { return __maimiMemoryValues.get(key) ?? null; },
  setItem(key, value) {
    const text = String(value);
    if (text.length > 1048576) return;
    if (__maimiMemoryValues.size >= 32 && !__maimiMemoryValues.has(key))
      __maimiMemoryValues.delete(__maimiMemoryValues.keys().next().value);
    __maimiMemoryValues.set(key, text);
  },
  removeItem(key) { __maimiMemoryValues.delete(key); }
};
"""
MEDIA_SOURCE = """
export function safeMediaSrc(value: unknown): string | undefined {
  if (typeof value !== 'string' || value.length > 2048) return undefined
  try {
    const url = new URL(value, document.baseURI)
    if (url.origin === location.origin && ['http:', 'https:'].includes(url.protocol)) return value
  } catch {}
  return undefined
}"""

def write(path, value):
    path.write_text(value, encoding='utf-8', newline='\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('katex', type=Path)
    args = parser.parse_args()
    root = args.source.resolve()
    katex = args.katex.resolve()
    if json.loads((katex/'package.json').read_text('utf8'))['version'] != '0.18.10':
        raise ValueError('Unexpected KaTeX release')
    entry = json.dumps((katex/'dist/katex.mjs').as_posix())
    for name, version, commit in [('genui','0.15.0','37c083b7747f5ef4509515fd5d1eb4e972c0e1a5'), ('sidebar','0.28.2','b0e0fab50e68f54b07a1fc1e7898030409c7d979')]:
        pkg = root/name
        if json.loads((pkg/'SOURCE.json').read_text('utf8'))['commit'] != commit or json.loads((pkg/'package.json').read_text('utf8'))['version'] != version:
            raise ValueError('Upstream release differs')
        config = (pkg/'tsdown.config.ts').read_text('utf8')
        if name == 'genui':
            target = "'node_modules/katex/dist/katex.min.css'"
            if config.count(target) != 1:
                raise ValueError('Upstream stylesheet pin differs')
            config = config.replace(target, json.dumps((katex/'dist/katex.min.css').as_posix()))
        write(pkg/'tsdown.pinned.ts', config)
        selector = "['client', 'assets/standalone-runtime', 'assets/mermaid'].includes(Object.keys(c.entry || {})[0])" if name == 'genui' else "Object.keys(c.entry || {}).includes('mermaid')"
        code = """
import configs from './tsdown.pinned.ts'
const selected = configs.filter(c => SELECTOR)
if (selected.length !== COUNT) throw Error('Unexpected upstream browser bundle recipe')
const pinnedKatex = {
  name: 'maimi-pinned-katex',
  enforce: 'pre',
  resolveId(id: string) { if (id === 'katex') return PATCHED; return null }
}
for(const config of selected) config.plugins = [pinnedKatex, ...(config.plugins || [])]
""".replace('SELECTOR', selector).replace('COUNT', '3' if name=='genui' else '1').replace('PATCHED',entry)
        write(pkg/'tsdown.enterprise.ts',code+'\nexport default selected\n')
        if name == 'genui':
            # Build the corporate managed client as its own source derivative.
            # Apply the identical .9 same-origin media and per-factory memory policy.
            value_path = pkg/'src/client/genui-runtime/value-utils.ts'
            original = value_path.read_text('utf8')
            begin = original.index('export function safeMediaSrc(')
            end = original.index('\n}',begin)+2
            if 'return normalized' not in original[begin:end]:
                raise ValueError('Upstream URL function differs')
            managed = original[:begin]+MEDIA_SOURCE.strip()+original[end:]
            write(pkg/'maimi-value-utils.source', managed)
            policy = """
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'
const managed = selected.find(c => Object.keys(c.entry || {})[0] === 'client')
managed.outDir = 'managed-output'
managed.define = {...managed.define, localStorage: '__maimiMemoryStorage'}
managed.outputOptions = {...managed.outputOptions, intro: managed.outputOptions.intro + MEMORY}
managed.plugins.unshift({
  name: 'maimi-same-origin-media',
  enforce: 'pre',
  transform(source: string, id: string) {
    if (!id.replaceAll('\\\\','/').endsWith('/src/client/genui-runtime/value-utils.ts')) return;
    if (createHash('sha256').update(source.replaceAll('\r\n','\n')).digest('hex') !== DIGEST) throw Error('Unexpected media policy source');
    return readFileSync(fileURLToPath(new URL('./maimi-value-utils.source', import.meta.url)), 'utf8');
  }
})
export default [managed]
"""
            # Preserve the JS escape sequences in the generated regular strings.
            policy = policy.replace("source.replaceAll('\r\n','\n')", "source.replaceAll('\\r\\n','\\n')")
            digest = hashlib.sha256(original.encode()).hexdigest()
            write(pkg/'tsdown.managed.ts',code+policy.replace('MEMORY',json.dumps(MEMORY_STORAGE)).replace('DIGEST',json.dumps(digest)))


if __name__ == '__main__':
    main()
