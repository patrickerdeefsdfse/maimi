(()=>{'use strict'; const elapsed=document.getElementById('elapsed');if(elapsed){const begin=performance.now();setInterval(()=>{if(!document.hidden)elapsed.textContent='已等待 '+Math.floor((performance.now()-begin)/1000)+' 秒';},1000);}
 globalThis.maimiEntryStage=(step)=>{document.querySelectorAll('.steps li').forEach((el,i)=>{el.classList.toggle('current',i===step);el.classList.toggle('done',i<step);if(i===step)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current');});};
})();
