// DSH still owns authorization, source versions, PDF validation and preview cache.
// Only its conversion backend uses the authenticated desktop office bridge.
import path from 'node:path';
import { PROTOCOL, runEngine } from './transport.mjs';

export async function createConverter(options = {}) {
  const stopped = new AbortController();
  const config = { endpoint: process.env.MEGMEET_OFFICE_ENDPOINT,
    token: process.env.MEGMEET_OFFICE_TOKEN, timeoutMs: Math.min(options.timeoutMs || 240000, 300000) };
  return {
    async render({ inputPath, outputPath }, signal) {
      if (!path.isAbsolute(inputPath) || !path.isAbsolute(outputPath)
          || path.dirname(inputPath) !== path.dirname(outputPath)) throw Error('OFFICE_PREVIEW_PATH_INVALID');
      const result = await runEngine(config, { protocol: PROTOCOL, operation: 'convert',
        workspace: path.dirname(inputPath), input: path.basename(inputPath),
        output: path.basename(outputPath), options: {} },
      AbortSignal.any([stopped.signal, signal].filter(Boolean)));
      return { missingFonts: Array.isArray(result?.missingFonts) ? result.missingFonts : [] };
    },
    async dispose() { stopped.abort(); },
  };
}
