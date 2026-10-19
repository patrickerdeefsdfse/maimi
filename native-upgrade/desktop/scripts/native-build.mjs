import fs from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { inspectSource, stageSource, makeBuildPlan } from '../src/native-build.mjs';

const { values, positionals } = parseArgs({ allowPositionals: true, options: { source: { type: 'string' }, output: { type: 'string' }, state: { type: 'string' }, pnpm: { type: 'string' }, lock: { type: 'string' } } });
try {
  const action = positionals[0];
  if (positionals.length !== 1 || !['inspect', 'stage', 'plan'].includes(action)) throw Error('EXPECTED_INSPECT_STAGE_OR_PLAN');
  const source = path.resolve(values.source ?? path.join(import.meta.dirname, '../../upstream/source/deepseek-harness-687f282e8625c6b8d11ff58211a90524be2e4317'));
  const pins = JSON.parse(fs.readFileSync(values.lock ?? path.join(import.meta.dirname, '../upstream-source-lock.json'), 'utf8'));
  let result = inspectSource(source, pins);
  if (action === 'stage') {
    if (!values.output || !path.isAbsolute(values.output)) throw Error('ABSOLUTE_NEW_OUTPUT_REQUIRED');
    result = stageSource(source, values.output, pins);
  } else if (action === 'plan') {
    result = makeBuildPlan({ source, stage: values.output, state: values.state, pnpmEntry: values.pnpm });
  }
  process.stdout.write(JSON.stringify(result) + '\n');
} catch (error) {
  process.stdout.write(JSON.stringify({ status: 'STOP', reason: /^[A-Z0-9_]+$/.test(error.message) ? error.message : 'LOCAL_INPUT_UNAVAILABLE', services_changed: false }) + '\n');
  process.exitCode = 1;
}
