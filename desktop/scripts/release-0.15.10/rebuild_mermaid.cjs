// Build only the two affected standalone products from Mermaid's published
// dependency-externalized core, and the two KaTeX-only dynamic modules. Keep
// the existing chunk names because the shipped ESM entry references them.
const fs = require('node:fs');
const path = require('node:path');
const [compilerDir, runtimeModules, outputDir] = process.argv.slice(2);
const esbuild = require(path.resolve(compilerDir, 'node_modules/esbuild'));
const readJSON = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const mermaid = path.resolve(runtimeModules, 'mermaid');
const katex = path.resolve(runtimeModules, 'katex');
const build = async () => {
  const source = readJSON(path.join(mermaid, 'package.json'));
  const math = readJSON(path.join(katex, 'package.json'));
  if (esbuild.version !== '0.25.12' || source.version !== '11.17.2' || math.version !== '0.18.10')
    throw new Error('Build input versions differ from qualified inputs');
  for (const minify of [false, true]) {
    const suffix = minify ? '.min' : '';
    const common = { bundle: true, platform: 'browser', keepNames: true, minify, metafile: true };
    const standalone = await esbuild.build({
      ...common,
      format: 'iife',
      globalName: '__esbuild_esm_mermaid_nm["mermaid"]',
      footer: {js: 'globalThis["mermaid"] = globalThis.__esbuild_esm_mermaid_nm["mermaid"].default;'},
      entryPoints: [path.join(mermaid, 'dist/mermaid.core.mjs')],
      alias: {katex: path.join(katex, 'dist/katex.mjs')},
      define: {'process.env.NODE_ENV':'"production"'},
      external: ['require', 'fs', 'path'],
      outfile: path.join(outputDir, 'mermaid' + suffix + '.js'),
    });
    if (!Object.keys(standalone.metafile.inputs).some(p => p.replaceAll('\\','/').endsWith('/katex/dist/katex.mjs')))
      throw new Error('Missing externalized KaTeX input');
    const family = path.join('chunks', 'mermaid.esm' + suffix);
    const oldChunks = fs.readdirSync(path.join(mermaid, 'dist', family)).filter(p => /^katex-.*\.mjs$/.test(p));
    if (oldChunks.length !== 1) throw new Error('Unexpected KaTeX chunk graph');
    await esbuild.build({
      ...common,
      format:'esm',
      entryPoints:[path.join(katex,'dist/katex.mjs')],
      outfile:path.join(outputDir,family,oldChunks[0])
    });
    fs.writeFileSync(path.join(outputDir, 'inputs'+suffix+'.json'), JSON.stringify(standalone.metafile.inputs));
  }
};
build().catch(error => {console.error(error.message); process.exitCode = 1;});
