// Version-guarded native adaptation. Sidebar keeps supplied 3.png; revision 5
// uses the user-requested transparent mascot at half the previous hero size.
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequestPlan, installEnterprise } from './enterprise-native.mjs';
import { adaptPinnedWorkspace, workspaceSourceSha256 } from './workspace-pinned-overlay.mjs';

const expected = '0.1.6-alpha.2';
const here = path.dirname(fileURLToPath(import.meta.url));
const root = process.argv[2] ?? '/opt/dsh/node_modules/@deepseek-ai';
const metadataPath = process.argv[3] ?? path.join(here, 'brand-adaptation.json');
const sha = value => createHash('sha256').update(value).digest('hex');
const originalHashes = {
  'dsh-client-ui-brand-official': '22bb7e181a8d93372e1d14927b201612b09a02c84ea3d0f36933cfff1de47d71',
  'dsh-client-ui-conversation': 'c02a68ebd98c7f32f70ab3302a6a6f5304c61717f28e87f616b070cbf105dabe',
  'dsh-client-ui-workspace': workspaceSourceSha256,
};
const assetHashes = {
  'brand-3-r4.png': 'e6ec56513e871d1e823b5e524aaf864c5116b4292ad48b18730e83b6f280d6fa',
  'hero-person-r5.png': '03668756d3b25718d909bdcf49d86f4832d27b200c09dc41ee59439b208c3235',
};
const sources = {};
for (const [name, digest] of Object.entries(originalHashes)) {
  const dir = path.join(root, name);
  const pkg = JSON.parse(readFileSync(path.join(dir, 'package.json'), 'utf8'));
  if (pkg.version !== expected) throw Error(`Review branding for ${name}@${pkg.version}`);
  const source = readFileSync(path.join(dir, 'lib/client.js'), 'utf8');
  if (sha(source) !== digest) throw Error(`Review upstream branding contract: ${name}`);
  sources[name] = source;
}
const images = {};
for (const [name, digest] of Object.entries(assetHashes)) {
  const bytes = readFileSync(path.join(here, 'assets', name));
  if (sha(bytes) !== digest) throw Error(`Brand asset differs from supplied image: ${name}`);
  images[name] = 'data:image/png;base64,' + bytes.toString('base64');
}

sources['dsh-client-ui-brand-official'] = `window.__ModuleLoader__.load({
  id: "@deepseek-ai/dsh-client-ui-brand-official",
  factory: require => {
    const { jsx } = require("react/jsx-runtime");
    const mark = ({ size }) => jsx("span", {
      "aria-label": "MEGMEET",
      style: { display: "inline-flex", width: size, height: size, flex: "none" },
      children: jsx("img", {
        src: ${JSON.stringify(images['brand-3-r4.png'])}, alt: "", width: size, height: size,
        "data-megmeet-sidebar-image": "r4",
        style: { display: "block", width: "100%", height: "100%", objectFit: "contain" }
      })
    });
    const wordmark = () => jsx("span", {
      "aria-label": "MEGMEET", "data-megmeet-brand-wordmark": "r4",
      style: { fontWeight: 700, letterSpacing: "0.02em", whiteSpace: "nowrap" }, children: "MEGMEET"
    });
    const hero = () => jsx("img", {
      src: ${JSON.stringify(images['hero-person-r5.png'])}, alt: "MEGMEET", width: 84, height: 84,
      "data-megmeet-hero-image": "r5",
      style: { display: "block", width: 84, height: 84, objectFit: "contain", flex: "none" }
    });
    return {
      inject: ["slots"],
      apply(ctx) {
        for (const [name, component] of [
          ["sidebar.brand.mark", mark], ["sidebar.brand.name", wordmark],
          ["conversation.hero.brand.mark", hero]
        ]) ctx.slots.inject(name, () => ctx.slots.register({ name }, component));
        (${installEnterprise.toString()})(ctx, require, (${createRequestPlan.toString()}));
      }
    };
  }
});
`;

let conversation = sources['dsh-client-ui-conversation'];
const title = 'children: t("hero.headline")';
const badge = /, \(0, react_jsx_runtime\.jsx\)\("span", \{\s*className: HeroShell_module_css_default\.previewBadge,\s*children: t\("hero\.preview"\)\s*\}\)/g;
if (conversation.split(title).length !== 2 || [...conversation.matchAll(badge)].length !== 1) {
  throw Error('Review upstream hero headline and Preview contracts');
}
conversation = conversation.replace(title, 'children: "电亮AI新纪元"').replace(badge, '');
sources['dsh-client-ui-conversation'] = conversation;
sources['dsh-client-ui-workspace'] = adaptPinnedWorkspace(sources['dsh-client-ui-workspace']);

// Validate every input and replacement before changing any installed module.
const adaptations = [];
for (const [name, source] of Object.entries(sources)) {
  writeFileSync(path.join(root, name, 'lib/client.js'), source);
  adaptations.push({ name: 'megmeet-brand-r5', package: '@deepseek-ai/' + name,
    originalSha256: originalHashes[name], adaptedSha256: sha(source) });
}
writeFileSync(metadataPath, JSON.stringify({
  dsh: expected, branding: 'megmeet-assets-r5', assets: assetHashes, adaptations,
  enterpriseAdapter: { version: '3.3.0', sha256: sha(readFileSync(path.join(here, 'enterprise-native.mjs'))) },
  workspaceAdapter: { version: '1.0.0', sha256: sha(readFileSync(path.join(here, 'workspace-pinned-overlay.mjs'))) },
}, null, 2) + '\n');
console.log('MEGMEET_NATIVE_BRAND_R5: PASS');
