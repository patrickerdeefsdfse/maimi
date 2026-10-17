// Company-approved chat model. Search has a separate provider contract.
export const COMPANY_CHAT_MODEL = 'deepseek-v4.1-flash';
export const isCompanyChatModel = value => value === COMPANY_CHAT_MODEL;
// Bounded, ASCII-only startup transport; these values are model metadata.
export function readCatalog(env) {
  const countText=env.CLOUD_MODEL_CATALOG_COUNT||'';
  if (!/^[1-9]$/.test(countText)) throw new Error('cloud catalog chunk count invalid');
  const count=Number(countText);
  const expected=new Set(['CLOUD_MODEL_CATALOG_COUNT', ...Array.from({length:count},(_,i)=>'CLOUD_MODEL_CATALOG_'+i)]);
  if (Object.keys(env).some(k=>k.startsWith('CLOUD_MODEL_CATALOG')&&!expected.has(k))) throw new Error('cloud catalog unexpected chunk');
  const chunks=[];
  let total=0;
  for (let i=0;i<count;i++) {
    const part=env['CLOUD_MODEL_CATALOG_'+i];
    if (typeof part!=='string'||!part.length||part.length>60000||!/^[\x20-\x7e]+$/.test(part)) throw new Error('cloud catalog chunk invalid');
    total+=part.length;
    if (total>524288) throw new Error('cloud catalog total invalid');
    chunks.push(part);
  }
  return JSON.parse(chunks.join(''));
}

// The managed provider accepts an explicit empty list. Never substitute the
// upstream built-in catalog when an employee has no authorized models.
export function normalizeCatalog(value) {
  if (!value || !Array.isArray(value.data) || value.data.length > 1000)
    throw new Error('cloud catalog unavailable');
  const ids = new Set();
  const data = value.data.map(item => {
    if (!item || typeof item.id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(item.id)
      || ids.has(item.id) || (item.name != null && (typeof item.name !== 'string' || item.name.length > 500)))
      throw new Error('cloud catalog invalid');
    ids.add(item.id);
    const result = { id: item.id, name: item.name || item.id };
    if (Object.hasOwn(item, 'inputModalities')) {
      const modalities = item.inputModalities;
      if (!Array.isArray(modalities) || !modalities.includes('text') ||
          modalities.some(value => value !== 'text' && value !== 'image') ||
          new Set(modalities).size !== modalities.length)
        throw new Error('cloud model modalities invalid');
      result.inputModalities = ['text', 'image'].filter(value => modalities.includes(value));
    }
    return result;
  });
  if (data.length ? !ids.has(value.default_model) : value.default_model != null)
    throw new Error('cloud default model invalid');
  const authorized = data.filter(item => isCompanyChatModel(item.id));
  return { data: authorized, default_model: authorized.length ? COMPANY_CHAT_MODEL : null };
}

export const MANAGED_PROVIDER = 'deepseek-official';
export function providerPatch(catalog, modelBase) {
  if(!catalog.data.length)return {providers:{}};
  return { providers: { [MANAGED_PROVIDER]: { api: 'openai-completions', apiKeyEnv: 'MEGMEET_MODEL_TOKEN',
    displayName: '麦米企业模型', baseURL: modelBase,
    // Preserve the already deployed adapter's off/low/high/max contract so
    // existing selections and V3 request headers remain valid after upgrade.
    models: catalog.data.map(({inputModalities,...model})=>({...model,...(inputModalities?{input:inputModalities}:{}),
      reasoningEfforts:{off:null,low:'low',high:'high',max:'max'}})),
    reasoning:'high',
    // Keep inline history below the Gateway's 16 MiB JSON budget. Native DSH
    // offloads older images as conversations grow instead of bypassing limits.
    maxRequestImageBytes: 12 * 1024 * 1024,
    compat: {supportsDeveloperRole:false,thinkingFormat:'deepseek',supportsReasoningEffort:true} } } };
}

export function createCatalogUpdater(rpcArgs, modelBase) {
  let tail = Promise.resolve();
  return (value, { preserveDefault = false } = {}) => {
    // Validate and snapshot before queuing; an invalid update cannot poison
    // the queue, and a caller cannot mutate a pending update's metadata.
    const next = normalizeCatalog(value);
    const task = tail.then(async () => {
      await rpcArgs('settings/replace', { ns: 'llm-pi-ai', section: providerPatch(next, modelBase) });
      if (next.data.length && !preserveDefault) await rpcArgs('settings/update', { ns: 'agent-default-model',
        patch: { provider: MANAGED_PROVIDER, model: next.default_model } });
      return next;
    });
    tail = task.catch(() => {});
    return task;
  };
}
