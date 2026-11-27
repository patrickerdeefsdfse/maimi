/* Decorative only. No identity, network requests, or business content. */
(()=>{'use strict';
 const canvas=document.getElementById('starfield'),button=document.getElementById('motion');
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');let paused=reduced.matches;
 try{paused=paused||localStorage.getItem('maimi.cosmos.paused')==='1';}catch{}
 const ctx=canvas?.getContext('2d',{alpha:true});let width=0,height=0,stars=[],frame=0,last=0,previous=0;
 let seed=92741;const random=()=>{seed=(seed*1664525+1013904223)>>>0;return seed/4294967296;};
 function resize(){if(!ctx)return;width=innerWidth;height=innerHeight;const dpr=Math.min(devicePixelRatio||1,1.5);canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);stars=Array.from({length:Math.min(160,Math.max(65,Math.floor(width*height/9000)))},()=>({x:(random()-.5)*2.8,y:(random()-.5)*2.8,z:random()*1.8+.2,r:random()*.8+.3}));paint(0);}
 function paint(delta){if(!ctx)return;ctx.clearRect(0,0,width,height);const scale=Math.max(width,height)*.63,cx=width*.53,cy=height*.43;
  for(const s of stars){s.z-=delta*.012;if(s.z<.16)s.z=2;const x=cx+s.x*scale/s.z,y=cy+s.y*scale/s.z;if(x<0||x>width||y<0||y>height)continue;const radius=Math.min(2.1,s.r/s.z);ctx.globalAlpha=Math.min(.55,.15+.25/s.z);ctx.fillStyle=s.r>.75?'#c2ddff':'#d2d7ef';ctx.beginPath();ctx.arc(x,y,radius,0,Math.PI*2);ctx.fill();if(s.z<.55&&!paused){const tail=.0007/s.z;ctx.strokeStyle='#aedbff';ctx.lineWidth=.55;ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(x+(x-cx)*tail,y+(y-cy)*tail);ctx.stroke();}}ctx.globalAlpha=1;
 }
 function tick(t){frame=0;if(paused||document.hidden)return;if(t-last>=32){paint(previous?Math.min((t-previous)/1000,.08):0);previous=t;last=t;}frame=requestAnimationFrame(tick);}
 function sync(){document.documentElement.classList.toggle('paused',paused||document.hidden);if(button){button.textContent=paused?'播放星空':'暂停动效';button.setAttribute('aria-pressed',String(paused));}cancelAnimationFrame(frame);previous=0;if(!paused&&!document.hidden&&ctx)frame=requestAnimationFrame(tick);else paint(0);}
 button?.addEventListener('click',()=>{paused=!paused;try{localStorage.setItem('maimi.cosmos.paused',paused?'1':'0');}catch{}sync();});
 reduced.addEventListener('change',()=>{paused=reduced.matches;sync();});document.addEventListener('visibilitychange',sync);addEventListener('resize',resize);resize();sync();
 const elapsed=document.getElementById('elapsed');if(elapsed){const begin=performance.now();setInterval(()=>{if(!document.hidden)elapsed.textContent='已等待 '+Math.floor((performance.now()-begin)/1000)+' 秒';},1000);}
 globalThis.maimiEntryStage=(step)=>{document.querySelectorAll('.steps li').forEach((el,i)=>{el.classList.toggle('current',i===step);el.classList.toggle('done',i<step);if(i===step)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current');});};
})();
