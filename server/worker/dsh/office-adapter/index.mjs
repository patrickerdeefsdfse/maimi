// Versioned external Host extension. No DSH core file is patched.
import { defineTool } from '@deepseek-ai/dsh-tools';
import { registerOfficeTools } from './adapter.mjs';
export const name = 'megmeet-office-adapter';
export const inject = ['tools', 'fs'];
export function apply(ctx, config) { registerOfficeTools(ctx, config, defineTool); }
