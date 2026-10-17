from pathlib import Path
root=Path(__file__).resolve().parent
p=root/'native-tests/test-app/account-lease.cjs'
s=p.read_text(encoding='utf-8')
s=s.replace("const run=promisify(execFile);", "const run=promisify(execFile);global.__leaseTiming=[];const timedProbe=fn=>async(...args)=>{const start=performance.now();try{return await fn(...args);}finally{global.__leaseTiming.push({kind:fn.name,ms:Math.round(performance.now()-start)});}};")
s=s.replace('ownerEvidence=windowsOwnerEvidence,','ownerEvidence=timedProbe(windowsOwnerEvidence),')
s=s.replace("const {createProcessEvidenceProbe}=require('./process-evidence-probe.cjs');", "const rawFactory=require('./process-evidence-probe.cjs').createProcessEvidenceProbe;const createProcessEvidenceProbe=(...args)=>{const p=rawFactory(...args);return {...p,query:timedProbe(p.query)};};")
p.write_text(s,encoding='utf-8')
p=root/'native-tests/test-actual-host.cjs';s=p.read_text(encoding='utf-8')
s=s.replace('const statuses=[];', "const statuses=[],timings=[];const timed=async(kind,work)=>{const start=performance.now();try{return await work();}finally{timings.push({kind,ms:Math.round(performance.now()-start)});}};")
s=s.replace('host=await startHost(opts);', "host=await timed('start',()=>startHost(opts));")
s=s.replace('await current.stop();',"await timed('stop',()=>current.stop());")
s=s.replace("const result={schema:","const result={timings,lease_probes:global.__leaseTiming,schema:")
p.write_text(s,encoding='utf-8')
p=root/'native-tests/test-history-incremental.mjs';s=p.read_text(encoding='utf-8')
# Only symlink creation is unsupported on this Windows host. Keep the
# interrupted staging and invalid-fence assertions running on Windows.
old="await symlink(target,path.join(g.state,home,'manifest.json'));await assert.rejects(g.migration.admit(),e=>e.code==='UNSAFE_FILE');"
new="try{await symlink(target,path.join(g.state,home,'manifest.json'));await assert.rejects(g.migration.admit(),e=>e.code==='UNSAFE_FILE');}catch(error){if(process.platform!=='win32'||error.code!=='EPERM')throw error;console.log('WINDOWS_SYMLINK_FIXTURE_UNAVAILABLE: this assertion is covered on Linux');}"
s=s.replace(old,new)
p.write_text(s,encoding='utf-8')
print('TEST_ONLY_TIMING_AND_WINDOWS_FIXTURE_ADAPTED')
