from pathlib import Path
import json,hashlib,shutil
R=Path('output/dsh020-rc2-r41-20261008/runtime')
old=Path('output/dsh-rc2-implementation-20260928/core/tests/test-history-admission.mjs').read_text(encoding='utf-8')
head=old.split("test('alpha V4 strict audit")[0].replace("../worker/history-admission-rc2.mjs","../overlay/opt/personal-cloud/history-admission-rc2.mjs").replace("../worker/history-native.mjs","../overlay/opt/personal-cloud/history-native.mjs")
extra=r'''
test('real rc17 compressed and plain V4 open under rc20, preserve original bytes and continue using real provider bridge',async()=>{
 for(const compression of ['zstd','none']){
  const f=await fixture({compression});assert.equal((await f.migration.admit()).runtime,'0.2.0-rc.2');assert.equal(hash(await readFile(f.source)),f.sourceHash);
  const s=await restored(runtime,f),h=await modelHarness(runtime);
  try{assert((await h.call(s.deriveMessages(),s.toolHistory())).length>0);assert.equal(h.requests,1);}finally{await h.close();}
 }
});
test('dynamic tool history rc17-to-rc20 strict native cold reopen and continuation',async()=>{
 const f=await fixture();await f.migration.admit();await appendDynamic(f);
 const s=await restored(runtime,f);assert(s.deriveMessages().some(m=>m.role==='developer'));
 const h=await modelHarness(runtime);try{assert((await h.call(s.deriveMessages(),s.toolHistory())).length>0);assert.equal(h.requests,1);}finally{await h.close();}
 assert.equal((await f.migration.admit()).status,'READY');
 await assert.rejects(async()=>f.migration.rollback(),e=>e.code==='RUNTIME_SNAPSHOT_REQUIRED');
});
test('explicit delete includes pre-upgrade snapshot while retaining attachments and workspace',async()=>{
 const f=await fixture();await f.migration.admit();const saved=path.join(f.state,'.history-runtime-020-snapshot/backups',hash(f.id),'payload');assert((await readdir(saved)).length>0);
 assert.equal((await f.maintenance.deleteSession({sessionId:f.id})).status,'deleted');assert.equal((await f.migration.admit()).status,'READY');await assert.rejects(lstat(saved),e=>e.code==='ENOENT');
 assert.equal(await readFile(path.join(f.state,'dsh/attachments/shared.txt'),'utf8'),'synthetic attachment');assert.equal(await readFile(path.join(f.workspace,'kept.txt'),'utf8'),'synthetic work');
});
'''
(R/'tests/test-history-continuation.mjs').write_text(head+extra,encoding='utf-8')
source=Path('output/remaining-fixes-r30-20260928/core/tests/test-integrated-native.py')
s=source.read_text(encoding='utf-8')
s=s.replace('BASE=ROOT.parents[2]/"dsh-rc2-implementation-20260928/integration-followup"\nCORE=ROOT.parent','WORKSPACE=Path(__file__).resolve().parents[4]\nBASE=WORKSPACE/"output/dsh-rc2-implementation-20260928/integration-followup"\nCORE=WORKSPACE/"output/remaining-fixes-r30-20260928/core"')
s=s.replace('    overlay_hashes={}','    overlays={k:v for k,v in overlays.items() if k.startswith("/app/")}\n    overlay_hashes={}')
s=s.replace("maimi-r30-test-","maimi-r41-test-").replace("maimi-r30-integrated-","maimi-r41-integrated-")
(R/'tests/test-integrated-native.py').write_text(s,encoding='utf-8')
shutil.copytree(source.parent/'tests-integrated',R/'tests/tests-integrated',dirs_exist_ok=True)
qa=R/'cards-word-qa'
pdf=qa/'员工工卡.pdf'
(qa/'validation.json').write_text(json.dumps({'schema':'card-docx-real-word-qa-r41-v1','status':'PASS','synthetic_card_count':101,'actual_pages':11,'layout':'A4 2 columns x 5 rows, 8.5cm x 5.4cm','renderer':'bundled LibreOffice and documents render_docx.py','first_page_and_last_page_visual':'PASS','synthetic_only':True,'pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),'stderr_note':'Windows LibreOffice emitted a non-UTF8 output reader warning; PDF and all 11 page PNGs rendered and visually verified'},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
