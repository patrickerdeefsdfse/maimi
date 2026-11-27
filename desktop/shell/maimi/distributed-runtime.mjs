// Cordis exports FiberState as a compile-time enum. ACTIVE is its third value in the pinned runtime.
const ACTIVE=2;

/** Each company release has its own disposable Cordis fiber, outside the user's DSH profile. */
export function createDistributedRuntime(ctx){
  const active=new Map();
  async function remove(key,digest){
    const previous=active.get(key);
    if(previous&&(!digest||previous.digest===digest)){await previous.fiber.dispose();active.delete(key);}
  }
  let pending=Promise.resolve(), closed=false;
  async function apply({action,row,content,code}){
    if(closed)throw Error('COMPANY_PLUGIN_RUNTIME_STOPPED');
    if(action==='remove'){await remove(row.plugin_key,row.sha256);return;}
    if(action!=='install')throw Error('COMPANY_PLUGIN_OPERATION_INVALID');
    const previous=active.get(row.plugin_key);
    if(previous?.digest===row.sha256)return;
    await remove(row.plugin_key);
    let plugin;
    if(row.kind==='skill'){
      const name='company-'+row.plugin_key;
      const existing=await ctx.skills.get(name);
      if(existing)throw Error('COMPANY_SKILL_NAME_IN_USE');
      plugin={name,inject:['skills'],apply(scoped){
        scoped.skills.register({name,description:row.name+' · '+row.description,source:'runtime',provider:'maimi-company',
          metadata:{displayName:row.name,version:row.version},content});
      }};
    } else {
      // The gateway only distributes operator-approved native digests. No package manager, lifecycle scripts or external dependencies.
      const mod=await import('data:text/javascript;base64,'+Buffer.from(code,'utf8').toString('base64')+'#'+row.sha256);
      if(typeof mod.apply!=='function')throw Error('COMPANY_PLUGIN_ENTRY_INVALID');
      plugin=mod;
    }
    const fiber=ctx.plugin(plugin);
    try{
      const started=await fiber;
      if(started.state!==ACTIVE) throw Error('COMPANY_PLUGIN_NOT_ACTIVE');
      if(row.kind==='skill'&&(await ctx.skills.get('company-'+row.plugin_key))?.provider!=='maimi-company')throw Error('COMPANY_PLUGIN_NOT_VISIBLE');
      active.set(row.plugin_key,{digest:row.sha256,fiber});
    }catch(e){await fiber.dispose();throw e;}
  }
  return {
    execute(input){const next=pending.then(()=>apply(input));pending=next.catch(()=>{});return next;},
    async close(){closed=true;await pending;for(const key of [...active.keys()])await remove(key);}
  };
}
