/* Small browser-local accent setting. No media uploads or server preferences. */
window.__ModuleLoader__.load({id:'maimi-appearance',factory:require=>{
 const {createElement,useState,useEffect}=require('react');
 const key='maimi.appearance.r73',choices={default:null,blue:'#3976bf',green:'#278269',purple:'#7857aa'};
 const eventName='maimi-appearance-changed';let style;
 function readChoice(){try{const value=localStorage.getItem(key);return Object.hasOwn(choices,value)?value:'default';}catch{return'default';}}
 function applyChoice(choice){
  if(!Object.hasOwn(choices,choice))choice='default';
  if(!style){style=document.createElement('style');style.dataset.maimiAppearance='r95';document.head.appendChild(style);}
  const value=choices[choice];
  // Native rc5's send control consumes button-info-fill/hover, not brand-primary.
  // Declare the semantic aliases at their root scope and on body: inherited
  // aliases computed on :root do not re-evaluate when only body changes.
  style.textContent=value?':root,body{'+[
   '--dsw-alias-brand-primary:'+value,
   '--dsw-alias-state-business-primary:'+value,
   '--dsw-alias-state-business-tertiary:color-mix(in srgb,'+value+' 14%,transparent)',
   '--dsw-alias-button-info-fill:'+value,
   '--dsw-alias-button-info-hover:color-mix(in srgb,'+value+' 84%,#000)',
   '--dsw-alias-button-primary-fill:'+value,
   '--dsw-alias-button-primary-hover:color-mix(in srgb,'+value+' 84%,#000)',
   '--dsw-alias-label-primary-foreground:#fff',
   '--dsw-focus-ring-color:'+value,
  ].map(rule=>rule+'!important;').join('')+'}':'';
 }
 function Theme(){
  const [choice,setChoice]=useState(readChoice);
  useEffect(()=>{const update=e=>setChoice(e.type===eventName&&Object.hasOwn(choices,e.detail)?e.detail:readChoice());window.addEventListener(eventName,update);window.addEventListener('storage',update);return()=>{window.removeEventListener(eventName,update);window.removeEventListener('storage',update);};},[]);
  const change=e=>{const next=e.target.value;if(!Object.hasOwn(choices,next))return;setChoice(next);applyChoice(next);try{localStorage.setItem(key,next);}catch{}window.dispatchEvent(new CustomEvent(eventName,{detail:next}));};
  return createElement('label',{style:{fontSize:12},title:'更改按钮和重点内容的颜色；只保存在当前浏览器'},'配色 ',createElement('select',{'aria-label':'麦米配色',value:choice,onChange:change},[['default','默认'],['blue','蓝色'],['green','绿色'],['purple','紫色']].map(([value,text])=>createElement('option',{key:value,value},text))));
 }
 return{inject:['slots'],apply(ctx){
  // Restore before a conversation header mounts, including a fresh tab/start page.
  ctx.effect(()=>{applyChoice(readChoice());const update=()=>applyChoice(readChoice());window.addEventListener('storage',update);return()=>{window.removeEventListener('storage',update);style?.remove();style=undefined;};},'maimi appearance');
  const name='conversation.session.header.actions';ctx.slots.inject(name,()=>ctx.slots.register({name,id:'maimi-appearance',order:200},Theme));
 },Theme};
}});
