'use strict';
// Only the unpackaged Electron verification entry can load this fixed script.
// Every message/file below is synthetic, every mutation is local development.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
module.exports = async function ({window, requestNative, user, evidence}) {
  const wc = window.webContents;
  const run = code => wc.executeJavaScript(code, true);
  const results = [];
  const passed = name => { results.push(name); console.log(name + ': PASS'); };
  async function wait(expression, label, limit = 50) {
    for (let n = 0; n < limit; n++) { if (await run(expression)) return; await delay(300); }
    throw Error(label);
  }
  async function capture(name) {
    await run('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
    fs.writeFileSync(path.join(evidence, name + '.png'), (await wc.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG());
  }
  const click = async label => {
    await wait(`(() => {const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()===${JSON.stringify(label)});return b&&!b.disabled;})()`, 'button not ready');
    return run(`(() => { [...document.querySelectorAll('button')].find(x=>x.textContent.trim()===${JSON.stringify(label)}).click(); return true; })()`);
  };
  try {
    await wait('!!document.querySelector("[data-megmeet-cloud-entry]")', 'cloud entry missing');
    const logo = await run(`(() => {
      const i=document.querySelector('[data-megmeet-hero-image]');const r=i.getBoundingClientRect();
      const c=document.createElement('canvas');c.width=i.naturalWidth;c.height=i.naturalHeight;
      c.getContext('2d').drawImage(i,0,0);const a=c.getContext('2d').getImageData(0,0,1,1).data[3];
      return {width:r.width,height:r.height,transparentCorner:a===0,title:document.body.innerText.includes('电亮AI新纪元'),preview:document.body.innerText.includes('Preview')};
    })()`);
    assert.equal(logo.width,168);assert.equal(logo.height,168);assert.equal(logo.transparentCorner,true);assert.equal(logo.title,true);assert.equal(logo.preview,false);
    passed('DESKTOP_HERO_SUPPLIED_BRAND_3_LARGE');
    await capture('hybrid-hero');
    await run('document.querySelector("[data-megmeet-cloud-entry]").click()');
    await wait('!!document.querySelector("[data-megmeet-cloud-panel]")','cloud panel missing');
    if (user.user_no === 'LOCAL-FIXTURE-B') {
      await wait('document.body.innerText.includes("暂无已授权")','B sees unexpected workspaces');
      passed('DESKTOP_B_NO_DEPARTMENT_AUTOGRANT');await capture('hybrid-no-grant');
      await click('关闭');
    } else {
      await wait('document.querySelector("select[aria-label=选择部门云工作台]")?.options.length>1','workspace list missing');
      await run(`(() => {const s=document.querySelector('select[aria-label="选择部门云工作台"]');s.value=s.options[1].value;s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
      await wait('document.querySelector("select[aria-label=选择企业插件]")?.options.length>1','plugin list missing');
      await run(`(() => {const s=document.querySelector('select[aria-label="选择企业插件"]');s.value=s.options[1].value;s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
      const marker = 'SYNTHETIC_CLOUD_PANEL_' + Date.now();
      await run(`(() => {const e=document.querySelector('textarea[aria-label="云工作台消息"]');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(e,${JSON.stringify(marker)});e.dispatchEvent(new Event('input',{bubbles:true}));
        const f=document.querySelector('input[type=file][aria-label="选择要上传的文本附件"]');const d=new DataTransfer();d.items.add(new File(['SYNTHETIC_DESKTOP_DOCUMENT\\nSecond line'], 'desktop-proof.txt',{type:'text/plain'}));f.files=d.files;f.dispatchEvent(new Event('change',{bubbles:true}));})()`);
      await wait('!!document.querySelector("[data-megmeet-cloud-panel] input[type=checkbox]")','confirmation missing');
      assert.equal(await run(`[...document.querySelectorAll('button')].find(b=>b.textContent==='运行所选插件').disabled`),true);
      passed('DESKTOP_ATTACHMENT_REQUIRES_CONFIRMATION');
      await run('document.querySelector("[data-megmeet-cloud-panel] input[type=checkbox]").click()');
      await click('运行所选插件');
      await wait('!!document.querySelector("section[aria-label=云端会话] article")','cloud plugin result missing');
      assert.equal(await run(`document.querySelector('section[aria-label="云端会话"]').textContent.includes(${JSON.stringify(marker)})`),true);
      assert.equal(await run(`Object.keys(localStorage).some(k=>(localStorage.getItem(k)||'').includes(${JSON.stringify(marker)}))`),false);
      passed('DESKTOP_CLOUD_PLUGIN_REAL_UPLOAD_EXECUTION');passed('DESKTOP_CLOUD_PANEL_BODY_NOT_IN_LOCAL_STORAGE');
      await capture('hybrid-cloud-plugin');
      await click('共享给本部门授权成员（含本会话附件）');
      await wait('document.body.innerText.includes("已共享给此工作台获授权成员")','share result missing');
      await click('撤回共享');
      await wait('document.body.innerText.includes("仅自己可见")','withdraw result missing');
      passed('DESKTOP_OWNER_SHARE_AND_WITHDRAW');await click('关闭');
    }
    // Expose no secret to the renderer: this endpoint contains a safe projection.
    const identity = await (await requestNative('/__megmeet/desktop/session')).json();
    assert.equal(identity.user.user_no,user.user_no);
    assert.equal(JSON.stringify(identity).includes('access_token'),false);
    passed('DESKTOP_SAFE_IDENTITY_PROJECTION');
    if (user.user_no === 'LOCAL-FIXTURE-A') {
      await require('./native-plugin-desktop-probe.cjs')({window,requestNative,user,evidence});
      passed('DESKTOP_NATIVE_PLUGIN_ATTACHMENT_CHAIN');
    }
    fs.writeFileSync(path.join(evidence,'hybrid-ui.json'),JSON.stringify({user:user.user_no,logo,checks:results},null,2));
  } catch (error) {
    await capture('hybrid-ui-failure');
    fs.writeFileSync(path.join(evidence,'hybrid-ui-failure.txt'),await run('document.body.innerText.slice(0,4000)'));
    throw error;
  }
};
