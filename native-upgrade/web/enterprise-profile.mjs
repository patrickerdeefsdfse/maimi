// Enterprise policy layered over the complete, unmodified upstream profile.
export const REVIEWED_RUNTIME = '0.2.1-rc.2';
export function enterpriseProfile() {
  return [
    {id:'llm-deepseek',disabled:true},
    {id:'llm-deepseek-account',disabled:true},
    {id:'session-log-deepseek',config:{enabled:false,maxBytes:8*1024*1024}},
    {id:'session-telemetry-otel',config:{mode:'DISABLED'}},
    {id:'desktop-product-telemetry',disabled:true},
    {id:'product-analytics',disabled:true},
    // Keep upstream Office skills and the complete installed LibreOffice Kit.
    // The company template/service remain additional enterprise capabilities.
    {id:'ui-brand-official',disabled:true},
    {insert:[{id:'megmeet-company-ui',name:'file:///opt/dsh/plugins/company-ui/index.mjs'}]},
    {id:'workspace-controller',name:'file:///opt/personal-cloud/company-workspace-controller.mjs'},
  ];
}
