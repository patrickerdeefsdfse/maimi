// DSH loads the approved instruction body again for each invocation. No package code runs here.
export const name = 'maimi-company-skills';
export const inject = ['skills'];
const invocation = { modelInvocable: true, userInvocable: true };
const resourceBase = { kind: 'opaque', description: '麦米企业插件库' };

function publication(row) {
  return { plugin_key: row.plugin_key, version: row.version, sha256: row.sha256 };
}
function candidate(row) {
  return { name: 'company-' + row.plugin_key, description: row.name + ' · ' + row.description,
    invocation, source: 'company', provider: name, resourceBase, rank: 650, locator: publication(row) };
}

// The same gateway which owns the employee lease decides both listing and
// content access. A cached DSH candidate cannot survive a later admin review.
export function registerGuardedProvider(registry, provider, config) {
  registry.registerProvider(control => {
    const original=provider(control);
    async function permitted(signal) {
      const signals=[control.signal,AbortSignal.timeout(5000)];
      if (signal) signals.push(signal);
      const response=await fetch(config.endpoint+'/bundled', {
        method:'POST', redirect:'error', signal:AbortSignal.any(signals),
        headers:{Authorization:'Bearer '+config.token,'Content-Type':'application/json'}, body:'{}',
      });
      if (!response.ok) throw Error('COMPANY_SKILL_POLICY_UNAVAILABLE');
      const {skills}=await response.json();
      if (!Array.isArray(skills) || !skills.every(key=>typeof key==='string')) throw Error('COMPANY_SKILL_POLICY_INVALID');
      return new Set(skills);
    }
    return {
      name:original.name,
      async list(options={}) {
        let allowed;
        try { allowed=await permitted(options.signal); }
        catch { control.signal.throwIfAborted(); options.signal?.throwIfAborted(); return {candidates:[],complete:false}; }
        const rows=await original.list(options);
        return {candidates:rows.filter(row=>allowed.has(row.name)),complete:false};
      },
      async get(candidate,options={}) {
        if (!(await permitted(options.signal)).has(candidate.name)) throw Error('CONFIDENTIAL_OR_UNAUTHORIZED_SKILL');
        const result=await original.get(candidate,options);
        if (candidate.name!==result.name || !(await permitted(options.signal)).has(result.name)) throw Error('CONFIDENTIAL_OR_UNAUTHORIZED_SKILL');
        return result;
      },
    };
  });
}

export async function apply(ctx, config) {
  ctx.skills.registerProvider(control => {
    const signal = control.signal;
    const seen = new Map();
    const acknowledged = new Set();
    let flight, lastSuccess = 0, candidates = [];
    async function call(operation, body, requestSignal) {
      const signals = [signal, AbortSignal.timeout(5000)];
      if (requestSignal) signals.push(requestSignal);
      const response = await fetch(config.endpoint + '/' + operation, {
        method: 'POST', headers: { Authorization: 'Bearer ' + config.token, 'Content-Type': 'application/json' },
        redirect: 'error', signal: AbortSignal.any(signals), body: JSON.stringify(body),
      });
      if (!response.ok) {
        const error = Error('COMPANY_SKILL_UNAVAILABLE_' + response.status);
        error.status = response.status;
        throw error;
      }
      return response.json();
    }
    async function receipt(row, status) {
      await call('receipt', { ...publication(row), status });
    }
    async function refresh() {
      const { items, remove } = await call('list', {});
      if (!Array.isArray(items) || !Array.isArray(remove)) throw Error('COMPANY_SKILL_INVALID_CATALOG');
      const next = items.map(candidate);
      const current = new Set(items.map(row => row.plugin_key));
      for (const key of seen.keys()) if (!current.has(key)) seen.delete(key);
      // Acknowledge only instruction bundles whose exact release can actually be resolved.
      // An unavailable receipt is retried on the next catalog read.
      const pending = items.filter(row => seen.get(row.plugin_key) !== row.sha256 || !acknowledged.has(row.sha256));
      let cursor = 0;
      async function install() {
       while (cursor < pending.length) {
        signal.throwIfAborted();
        const row = pending[cursor++];
        try {
          if (seen.get(row.plugin_key) !== row.sha256) {
            const release = await call('get', publication(row));
            if (release.sha256 !== row.sha256 || !release.instructions?.trim()) throw Error('COMPANY_SKILL_INVALID_RELEASE');
            seen.set(row.plugin_key, row.sha256);
          }
          await receipt(row, 'installed');
          acknowledged.add(row.sha256);
        } catch {
          if (seen.get(row.plugin_key) !== row.sha256) {
            try { await receipt(row, 'failed'); } catch { /* A simultaneous withdrawal also rejects this report. */ }
          }
        }
       }
      }
      await Promise.all(Array.from({length: Math.min(4, pending.length)}, install));
      for (const row of remove) {
        acknowledged.delete(row.sha256);
        try { await receipt(row, 'removed'); } catch { /* Retry while the server still lists it. */ }
      }
      candidates = next.filter(row => seen.get(row.locator.plugin_key) === row.locator.sha256);
      lastSuccess = Date.now();
    }
    return {
      name,
      async list(options = {}) {
        options.signal?.throwIfAborted();
        if (Date.now() - lastSuccess >= 3000) {
          flight ??= refresh().finally(() => { flight = undefined; });
          try { await flight; } catch { candidates = []; }
        }
        options.signal?.throwIfAborted();
        return { candidates, complete: false }; // Next menu open rechecks grants without restarting DSH.
      },
      async get(item, options = {}) {
        const row = await call('get', item.locator, options.signal);
        if (item.name !== 'company-' + row.plugin_key || row.sha256 !== item.locator.sha256 ||
            row.version !== item.locator.version || !row.instructions?.trim()) throw Error('COMPANY_SKILL_RELEASE_CHANGED');
        // Only a real provider read may attest availability. Catalog prefetch sends installed.
        try {
          await call('receipt', { ...publication(row), status: 'usable' }, options.signal);
        } catch (error) {
          signal.throwIfAborted();
          options.signal?.throwIfAborted();
          if ([401,403,404].includes(error.status)) throw error;
          // Older gateways or a temporary receipt failure do not invalidate the authorized read.
          // A later invocation will try again.
        }
        return { name: item.name, description: item.description, invocation, source: 'company',
          provider: name, resourceBase, content: row.instructions };
      },
    };
  });
  if (config.manageBundledSkills) {
    const scoped={skills:{registerProvider:provider=>registerGuardedProvider(ctx.skills,provider,config)}};
    const plugins=await Promise.all([
      import('file:///opt/dsh/toolkit/enterprise-task-skills.mjs'),
      import('file:///opt/dsh/review-skills/index.mjs'),
      import('file:///opt/dsh/company-ppt-template/index.mjs'),
    ]);
    for (const plugin of plugins) await plugin.apply(scoped);
  }
}
