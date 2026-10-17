import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeCatalog, providerPatch} from './catalog.mjs';
import {resolveAdapterOptions} from '../../desktop-local/dist/0.8.1-company/MEGMEET AI-win32-x64/resources/runtime/node_modules/@deepseek-ai/dsh-llm-deepseek/lib/index.js';

test('unchanged native DSH resolver receives negotiated image capability and keeps unknown models text-only', () => {
  const input = {data:[{id:'synthetic-selected',name:'Selected',inputModalities:['text','image']},
    {id:'synthetic-unknown',name:'Unknown'}], default_model:'synthetic-unknown'};
  const patch = providerPatch(normalizeCatalog(input), 'http://gateway:8080/internal/personal-cloud/model/v1');
  const resolved = resolveAdapterOptions(patch);
  assert.deepEqual(resolved.models[0].inputModalities, ['text','image']);
  assert.deepEqual(resolved.models[1].inputModalities, ['text']);
  assert.equal(resolved.baseURL, patch.baseURL);
  assert.equal(resolved.protocol, 'chat-completions');
  assert.equal(resolved.maxInlineRequestImageBytes, 12 * 1024 * 1024);
  assert.equal(resolved.inlineImageOffloadByteQuantum, 4 * 1024 * 1024);
  assert.equal(resolved.maxImagesPerRequest, 8);
  assert.equal(resolved.imageOffloadCountQuantum, 2);
});
