import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeCatalog, createCatalogUpdater, readCatalog } from './catalog.mjs';

const modelBase = 'http://gateway:8080/internal/personal-cloud/model/v1';
const catalog = id => ({ data: [{ id, name: 'Model ' + id }], default_model: id });

test('empty employee catalog is preserved from startup through live provider settings', async () => {
  const empty = { data: [], default_model: null };
  const parsed = readCatalog({ CLOUD_MODEL_CATALOG_COUNT: '1', CLOUD_MODEL_CATALOG_0: JSON.stringify(empty) });
  assert.deepEqual(normalizeCatalog(parsed), empty);
  const calls = [];
  await createCatalogUpdater(async (...args) => calls.push(args), modelBase)(parsed);
  assert.deepEqual(calls, [['settings/update', { ns: 'llm-deepseek', patch: {
    protocol: 'chat-completions', apiKeyEnv: 'DEEPSEEK_API_KEY', baseURL: modelBase, models: [],
    maxInlineRequestImageBytes: 12 * 1024 * 1024, inlineImageOffloadByteQuantum: 4 * 1024 * 1024,
    maxImagesPerRequest: 8, imageOffloadCountQuantum: 2,
  } }]]);
});

test('settings calls use named ns/patch arguments and update default only after provider', async () => {
  const calls = [];
  const result = await createCatalogUpdater(async (...args) => calls.push(args), modelBase)(catalog('allowed'));
  assert.equal(result.default_model, 'allowed');
  assert.equal(calls[0][1].ns, 'llm-deepseek');
  assert.deepEqual(calls[0][1].patch.models, [{ id: 'allowed', name: 'Model allowed' }]);
  assert.deepEqual(calls[1], ['settings/update', { ns: 'agent-default-model',
    patch: { provider: 'deepseek-official', model: 'allowed' } }]);
  assert.equal(calls[0][1].request, undefined);
});

test('vision capability updates preserve the employee default selection', async () => {
  const calls = [];
  const update = createCatalogUpdater(async (method, args) => calls.push(args), modelBase);
  await update(catalog('company-first-model'), {preserveDefault:true});
  assert.deepEqual(calls.map(value => value.ns), ['llm-deepseek']);
  await update(catalog('ordinary-catalog-refresh'));
  assert.equal(calls.at(-1).ns, 'agent-default-model');
});

test('concurrent refreshes serialize complete updates and snapshot the input', async () => {
  const calls = [];
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const update = createCatalogUpdater(async (method, args) => {
    calls.push(args);
    if (calls.length === 1) await gate;
  }, modelBase);
  const second = catalog('second');
  const one = update(catalog('first'));
  const two = update(second);
  second.data[0].id = 'mutated';
  await Promise.resolve();
  assert.equal(calls.length, 1);
  release();
  await Promise.all([one, two]);
  assert.deepEqual(calls.map(x => x.ns), ['llm-deepseek', 'agent-default-model', 'llm-deepseek', 'agent-default-model']);
  assert.equal(calls[2].patch.models[0].id, 'second');
  assert.equal(calls[3].patch.model, 'second');
});

test('provider/default failures reject the refresh and leave subsequent revocation usable', async () => {
  for (const failedNs of ['llm-deepseek', 'agent-default-model']) {
    let fail = true;
    const calls = [];
    const update = createCatalogUpdater(async (method, args) => {
      calls.push(args);
      if (fail && args.ns === failedNs) { fail = false; throw new Error('native update failed'); }
    }, modelBase);
    await assert.rejects(update(catalog('old')), /native update failed/);
    await update({ data: [], default_model: null });
    assert.deepEqual(calls.at(-1).patch.models, []);
    if (failedNs === 'llm-deepseek') assert.equal(calls.length, 2);
  }
});

test('catalog validation rejects unauthorized default, duplicates, malformed or excessive models', () => {
  for (const bad of [null, {}, { data: [] , default_model: 'not-authorized' },
    { data: [{ id: 'allowed' }], default_model: 'foreign' },
    { data: [{ id: 'a' }, { id: 'a' }], default_model: 'a' },
    { data: [{ id: '../invalid?' }], default_model: '../invalid?' },
    { data: [{ id: 'a', name: {} }], default_model: 'a' },
    { data: Array.from({ length: 1001 }, (_, i) => ({ id: 'm' + i })), default_model: 'm0' },
  ]) assert.throws(() => normalizeCatalog(bad));
});

test('explicit verified image modalities reach native provider without inventing support for unknown models', async () => {
  const input = {data: [{id:'vision',name:'Vision',inputModalities:['image','text']},
    {id:'unknown',name:'Unknown'}, {id:'text',inputModalities:['text']}], default_model:'unknown'};
  const calls = [];
  const update = createCatalogUpdater(async (method,args) => calls.push(args), modelBase);
  const task = update(input);
  input.data[0].inputModalities.length = 0;
  await task;
  assert.deepEqual(calls[0].patch.models, [
    {id:'vision',name:'Vision',inputModalities:['text','image']},
    {id:'unknown',name:'Unknown'}, {id:'text',name:'text',inputModalities:['text']},
  ]);
});

test('malformed modalities cannot silently elevate image permission', () => {
  for (const inputModalities of [null, [], ['image'], ['text','audio'], ['text','text'], 'text,image', ['text',{}]]) {
    assert.throws(() => normalizeCatalog({data:[{id:'model',inputModalities}],default_model:'model'}),
      /cloud model modalities invalid/);
  }
});
