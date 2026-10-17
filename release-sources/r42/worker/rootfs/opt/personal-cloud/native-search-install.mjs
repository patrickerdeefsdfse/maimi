import { createOfficialFirstSearch } from './official-search-provider.mjs';

// Called only while assembling the native task-control service. No public RPC
// exposes issueCredential or the captured task generation. The runtime owner
// supplies the *actual* official class from its already-pinned rc.2 package.
export function installEnterpriseSearch(ctx, { DeepSeekSearchProvider, nativeScopes,
  issueCredential, releaseCredential, endpoint, recordOutcome = () => {} }) {
  const provider = createOfficialFirstSearch({ DeepSeekSearchProvider, endpoint,
    acquireScope: () => {
      const agent = ctx.agents.currentInitiator();
      const scope = nativeScopes.captureToolScope(agent);
      return issueCredential({ ...scope, purpose: 'web-search' });
    },
    releaseScope: releaseCredential,
    recordRequest: request => ctx.agents.currentInitiator().session.append('web/deepseek-search-llm-request', request),
    // Host may send these fixed codes to its bounded metadata audit. Do not
    // introduce an unregistered history event into existing employee sessions.
    recordOutcome });
  return ctx.web.registerSearchProvider(provider);
}
