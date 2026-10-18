window.__ModuleLoader__.load({
 id:'@megmeet/dsh-native-desktop-ui',
 factory:require=>{
  const {createElement:h}=require('react');
  return {inject:['slots'],apply(ctx){
   function CloudEntry({wide=true}){
    return h('a',{href:'https://platform-ai.megmeet.com/maimi/web/chat',target:'_blank',rel:'noopener noreferrer',title:'在浏览器打开云端工作区','aria-label':'在浏览器打开云端工作区','data-maimi-native-cloud-entry':'',style:{display:'flex',alignItems:'center',padding:'8px 12px',textDecoration:'none',color:'inherit',borderRadius:8}},wide?'云端工作区（浏览器）':'☁');
   }
   ctx.slots.inject('sidebar.footer.action',()=>ctx.slots.register({name:'sidebar.footer.action',id:'maimi-cloud-entry',order:0},CloudEntry));
  }};
 }
});
