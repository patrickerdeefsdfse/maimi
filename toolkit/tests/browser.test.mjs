import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {createBrowserDriver} from '../browser.mjs';
test('dedicated browser reads and edits only the owned fixture; stale elements never retarget',{skip:process.platform!=='win32'},async t=>{
 const server=http.createServer((req,res)=>{res.setHeader('Content-Type','text/html');res.end('<title>Owned browser fixture</title><input placeholder="Note"><button onclick="document.querySelector(\'input\').remove()">Remove</button><button onclick="document.body.dataset.clicked=1">Keep</button><p id="value"></p><script>document.querySelector("input").addEventListener("input",e=>document.querySelector("#value").textContent=e.target.value)</script>');});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const driver=createBrowserDriver({headless:true});
 t.after(async()=>{await driver.close();server.closeAllConnections();server.close();});
 const first=await driver.invoke('open',{url:'http://127.0.0.1:'+server.address().port});
 assert.equal(first.title,'Owned browser fixture');const input=first.elements.find(x=>x.name==='Note');
 const typed=await driver.invoke('fill',{ref:input.ref,text:'OWNED_FIXTURE_OK'});assert.match(typed.text,/OWNED_FIXTURE_OK/);
 await assert.rejects(driver.invoke('click',{ref:input.ref}),/STALE/);
 const image=await driver.invoke('screenshot',{});assert.equal(image.image.mimeType,'image/png');assert.ok(image.image.data.length>100);
 await assert.rejects(driver.invoke('open',{url:'file:///C:/Windows/win.ini'}),/HTTP_URL/);
});
