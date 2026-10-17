// Reviewed only against DSH 0.2.0-rc.2. This does not alter independent web search.
export const COMPANY_CHAT_MODEL = 'deepseek-v4.1-flash';
export const COMPANY_CHAT_PROVIDER = 'deepseek-official';
export const inject = ['llm'];
export const MANAGED_PLUGIN_MUTATIONS = Object.freeze(['setPluginEnabled','setBundleEnabled','removeBundle','installBundle','setVersionExemption','cancelInstall']);
const denied = () => { throw Object.assign(new Error('公司当前仅开放 DeepSeek V4.1 Flash，请使用企业模型。'), {code:'company_model_policy_refused'}); };
export function canonicalSelection(value) {
  if (!value || typeof value !== 'object') denied();
  return {...value, provider:COMPANY_CHAT_PROVIDER, model:COMPANY_CHAT_MODEL};
}
export function assertManagedAdapter(llm, expectedBase) {
  const adapter = llm.registration(COMPANY_CHAT_PROVIDER)?.adapter;
  if (!adapter || typeof adapter.current !== 'function' || typeof adapter.modelOf !== 'function' || typeof adapter.profileOf !== 'function') denied();
  const snapshot = adapter.current();
  const model = adapter.modelOf(snapshot, COMPANY_CHAT_PROVIDER, COMPANY_CHAT_MODEL);
  const profile = adapter.profileOf(snapshot, COMPANY_CHAT_PROVIDER);
  if (model?.baseUrl !== expectedBase || model?.api !== 'openai-completions' || profile?.apiKeyEnv !== 'MEGMEET_MODEL_TOKEN') denied();
  return snapshot;
}
export function apply(ctx, config) {
  if (config?.apiKeyEnv !== 'MEGMEET_MODEL_TOKEN' || typeof config.modelBase !== 'string'
      || !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}\/(?:v1|__megmeet\/model\/v1)$/.test(config.modelBase)
      || Number(new URL(config.modelBase).port) > 65535) denied();
  const llm = ctx.llm, base = config.modelBase;
  // Image admission asks for model capabilities before preparing a call. Old
  // history can still name its previous route; inspect the same managed model
  // that will receive the next request, without inventing image support.
  ctx.effect(() => {
    const own=Object.hasOwn(llm,'resolveModelInfo'),original=llm.resolveModelInfo;
    if(typeof original!=='function')denied();
    const resolve=function(_provider,_model,signal){
      assertManagedAdapter(llm,base);
      return original.call(this,COMPANY_CHAT_PROVIDER,COMPANY_CHAT_MODEL,signal);
    };
    llm.resolveModelInfo=resolve;
    return ()=>{if(llm.resolveModelInfo===resolve){if(own)llm.resolveModelInfo=original;else delete llm.resolveModelInfo;}};
  });
  // Prepare runs before adapter selection, capability lookup and the new request
  // header. Legacy session selections are normalized for this new request only.
  // Existing user/assistant history bytes and reasoning choice are untouched.
  for (const method of ['resolveCallConfig', 'prepareCall']) ctx.effect(() => {
    const own = Object.hasOwn(llm, method), original = llm[method];
    if (typeof original !== 'function') denied();
    const prepare = function(value, signal) {
      assertManagedAdapter(llm, base);
      return original.call(this, canonicalSelection(value), signal);
    };
    llm[method] = prepare;
    return () => { if (llm[method] === prepare) { if (own) llm[method] = original; else delete llm[method]; } };
  });
  ctx.effect(() => {
    const ownProviders=Object.hasOwn(llm,'listProviders'), ownModels=Object.hasOwn(llm,'listModels');
    const originalProviders=llm.listProviders, originalModels=llm.listModels;
    if(typeof originalProviders!=='function'||typeof originalModels!=='function')denied();
    const providers=function(){return originalProviders.call(this).filter(item=>item.id===COMPANY_CHAT_PROVIDER);};
    const models=async function(provider){
      if(provider!==COMPANY_CHAT_PROVIDER)return [];
      assertManagedAdapter(llm,base);
      return (await originalModels.call(this,provider)).filter(item=>item.id===COMPANY_CHAT_MODEL);
    };
    llm.listProviders=providers;llm.listModels=models;
    return ()=>{
      if(llm.listProviders===providers){if(ownProviders)llm.listProviders=originalProviders;else delete llm.listProviders;}
      if(llm.listModels===models){if(ownModels)llm.listModels=originalModels;else delete llm.listModels;}
    };
  });
  // Covers direct stream consumers as well as prepared main/subagent/title/
  // compaction calls, and fails closed if the same provider is edited to BYO.
  ctx.on('llm/stream', (options, next) => {
    if (options?.provider !== COMPANY_CHAT_PROVIDER || options?.model !== COMPANY_CHAT_MODEL) denied();
    assertManagedAdapter(llm, base);
    return next();
  }, {global:true, prepend:true});
  // Native service boundary also covers the desktop multiplexed RPC carrier.
  // Read-only inventory and ordinary settings (e.g. token-meter UI) stay usable.
  // Business skill selection is owned by the project bridge, not this manager.
  ctx.inject(['pluginManager'], child => child.effect(() => {
    const manager=child.pluginManager, saved=[];
    for(const method of MANAGED_PLUGIN_MUTATIONS){
      if(typeof manager[method]!=='function')denied();
      const own=Object.hasOwn(manager,method),original=manager[method];
      const refuse=()=>denied();manager[method]=refuse;saved.push({method,own,original,refuse});
    }
    return ()=>{for(const {method,own,original,refuse} of saved)if(manager[method]===refuse){if(own)manager[method]=original;else delete manager[method];}};
  }));
}
