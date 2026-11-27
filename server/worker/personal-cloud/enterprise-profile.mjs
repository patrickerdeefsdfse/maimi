// Enterprise policy layered over the complete, unmodified upstream profile.
export const REVIEWED_RUNTIME = '0.2.2-rc.5';
export function enterpriseProfile({ manageBundledSkills = false } = {}) {
  return [
    ...(process.env.MAIMI_WORKSPACE_STATE_BACKEND==='postgres'?[
      {id:'storage-domain',config:{backend:'json',routes:{workspace:'maimi-pg'}}},
      {insert:[{id:'maimi-pg-workspace-state',name:'file:///opt/personal-cloud/native-workspace-state.mjs'}]},
    ]:[]),
    {id:'llm-deepseek',disabled:true},
    // The stock service folds complete personas after registry hooks. Preserve
    // its implementation and configuration while retaining the company boundary.
    {id:'system-prompt',disabled:true},
    {insert:[{id:'maimi-system-prompt',name:'file:///opt/personal-cloud/company-system-prompt.mjs',config:{personaPrefix:''}}]},
    {id:'llm-deepseek-account',disabled:true},
    {id:'session-log-deepseek',config:{enabled:false,maxBytes:8*1024*1024}},
    {id:'session-telemetry-otel',config:{mode:'DISABLED'}},
    {id:'desktop-product-telemetry',disabled:true},
    {id:'product-analytics',disabled:true},
    {id:'ui-plugin-manager',disabled:true},
    // A browser on an employee computer cannot launch desktop apps on this cloud Host.
    {id:'ui-open-in-app',disabled:true},
    {id:'entry-customization',disabled:true},
    {id:'tool-plugin-manager',disabled:true},
    // Keep upstream Office skills and the complete installed LibreOffice Kit.
    // The company template/service remain additional enterprise capabilities.
    {id:'ui-brand-official',disabled:true},
    {insert:[{id:'megmeet-company-ui',name:'file:///opt/dsh/plugins/company-ui/index.mjs'}]},
    // Cordis patch.name asserts the existing name; it cannot replace it.
    {id:'workspace-controller',disabled:true},
    // Preserve the original Client half and package identity while the company
    // Host subclass continues to enforce the fixed /workspace policy.
    {insert:[{id:'maimi-workspace-client',name:'file:///opt/personal-cloud/workspace-client/index.mjs'}]},
    {id:'session-persistence-jsonl',disabled:true},
    {id:'attachment-local',disabled:true},
    {id:'office-kit',disabled:true},
    {id:'subprocess',disabled:true},
    // Native search requests bounded stdout from the remote execution service.
    {id:'tool-fs-search',config:{rawOutputMaxBytes:8*1024*1024}},
    {id:'bash-sandbox',disabled:true},
    {id:'fs-sandbox',disabled:true},
    {id:'skill-filesystem',config:{includeDefaultRoots:false,watch:false,bundledSkillDir:'/opt/dsh/skills'}},
    {insert:[
      ...(manageBundledSkills ? [] : [{id:'megmeet-enterprise-task-skills',name:'file:///opt/dsh/toolkit/enterprise-task-skills.mjs'}]),
      {id:'maimi-execution-providers',name:'file:///opt/personal-cloud/remote-execution-plugin.mjs'},
      {id:'maimi-isolated-office-kit',name:'file:///opt/personal-cloud/office-kit-bridge.mjs'},
      {id:'maimi-workspace-controller',name:'file:///opt/personal-cloud/company-workspace-controller.mjs'},
      {id:'maimi-pg-history',name:'file:///opt/personal-cloud/native-history-plugin.mjs'},
      {id:'maimi-nas-attachments',name:'file:///opt/personal-cloud/native-attachment-plugin.mjs'},
      {id:'maimi-managed-outputs',name:'file:///opt/personal-cloud/managed-outputs-plugin.mjs'},
      {id:'maimi-managed-plugins',name:'file:///opt/dsh/plugins/managed/r73/index.mjs',config:{surface:'web'}},
      {id:'maimi-context',name:'file:///opt/dsh/plugins/managed/r73/context/index.mjs',config:{surface:'web',workspaceRoot:'/workspace'}},
      {id:'maimi-documents',name:'file:///opt/dsh/plugins/managed/r73/files/index.mjs',config:{surface:'web'}},
      {id:'maimi-genui',name:'file:///opt/dsh/plugins/managed/r73/genui/index.mjs',config:{surface:'web'}},
    ]},
  ];
}
