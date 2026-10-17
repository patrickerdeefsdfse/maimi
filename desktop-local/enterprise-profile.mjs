// Explicit enterprise policy for the exact reviewed DSH runtime.
// OTel registry remains available to consumers; all export paths are disabled.
export const REVIEWED_RUNTIME = '0.2.0-rc.2';
export function enterpriseProfile({cloud = true} = {}) {
  return [
    {id:'llm-deepseek',disabled:true},
    {id:'llm-deepseek-account',disabled:true},
    {id:'session-log-deepseek',config:{enabled:false,maxBytes:8*1024*1024}},
    {id:'session-telemetry-otel',config:{mode:'DISABLED'}},
    {id:'desktop-product-telemetry',disabled:true},
    {id:'product-analytics',disabled:true},
    {id:'skill-office',config:{cli:false}},
  ];
}
