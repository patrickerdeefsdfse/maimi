export const COMPANY_CHAT_PROVIDER = 'deepseek-official';
export const inject = ['llm'];
export const MANAGED_PLUGIN_MUTATIONS = Object.freeze(['setPluginEnabled','setBundleEnabled','removeBundle','installBundle','setVersionExemption','cancelInstall']);
function denied() {
 throw Object.assign(new Error('请选择本人 TokenKey 已授权的企业模型。'), {code:'company_model_policy_refused'});
}
export function apply(ctx, config) {
 const base = config?.modelBase;
 if (config?.apiKeyEnv !== 'MEGMEET_MODEL_TOKEN' || typeof base !== 'string' ||
     !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}\/(?:v1|__megmeet\/model\/v1)$/.test(base) ||
     Number(new URL(base).port) > 65535) denied();
 const llm = ctx.llm;
 const historyAdapters = new WeakSet();
 function bindAttachmentHistory(adapter) {
   if (historyAdapters.has(adapter) || !ctx.get('attachments')?.streamWithHistory) return;
   ctx.effect(() => {
     const own = Object.hasOwn(adapter, 'prepareCall'), original = adapter.prepareCall;
     async function prepare(...args) {
       const call = await original.apply(this,args);
       return {...call, stream(options) {
         const store = ctx.get('attachments');
         return store?.streamWithHistory ? store.streamWithHistory(options, value => call.stream(value)) : call.stream(options);
       }};
     }
     adapter.prepareCall = prepare;
     historyAdapters.add(adapter);
     return () => {
       historyAdapters.delete(adapter);
       if(adapter.prepareCall===prepare){if(own)adapter.prepareCall=original;else delete adapter.prepareCall;}
     };
   });
 }
 function inventory() {
   const adapter = llm.registration(COMPANY_CHAT_PROVIDER)?.adapter;
   if (!adapter) return [];
   bindAttachmentHistory(adapter);
   const snapshot = adapter.current();
   const profile = adapter.profileOf(snapshot, COMPANY_CHAT_PROVIDER);
   if (profile.apiKeyEnv !== 'MEGMEET_MODEL_TOKEN') denied();
   return snapshot.models.getModels(COMPANY_CHAT_PROVIDER).filter(m => m.baseUrl === base && m.api === 'openai-completions');
 }
 function check(provider, model) {
   if (provider !== COMPANY_CHAT_PROVIDER || !inventory().some(m => m.id === model)) denied();
 }
 function wrap(method, decorate) {
   ctx.effect(() => {
     const own = Object.hasOwn(llm,method), original = llm[method];
     if (typeof original !== 'function') denied();
     const wrapped = decorate(original);
     llm[method] = wrapped;
     return () => { if (llm[method] === wrapped) { if (own) llm[method] = original; else delete llm[method]; } };
   });
 }
 wrap('listProviders', original => function(...args) {
   return original.apply(this,args).filter(p => p.id === COMPANY_CHAT_PROVIDER);
 });
 wrap('listModels', () => async provider => provider !== COMPANY_CHAT_PROVIDER ? [] :
    inventory().map(m => ({provider,id:m.id,name:m.name,inputModalities:[...m.input]})));
 wrap('resolveModelInfo', original => function(provider, model, ...args) {
   check(provider,model); return original.call(this,provider,model,...args);
 });
 for (const method of ['resolveCallConfig','prepareCall']) wrap(method, original => function(value,...args) {
   check(value?.provider,value?.model); return original.call(this,value,...args);
 });
 ctx.on('llm/stream',(options,next) => { check(options?.provider,options?.model); return next(); }, {global:true,prepend:true});
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
