from pathlib import Path
p=Path('output/dsh020-rc2-r41-20261008/runtime/tests/test-history-continuation.mjs')
s=p.read_text(encoding='utf-8')
a=s.index('async function appendDynamic(f)');b=s.index('async function modelHarness(root)',a)
part=s[a:b].replace('async function appendDynamic(f)','async function appendDynamic(f,root=runtime)').replace('backend(runtime,','backend(root,').replace('restored(runtime,','restored(root,').replace("mod(runtime,'dsh-llm')","mod(root,'dsh-llm')")
s=s[:a]+part+s[b:]
s=s.replace("const f=await fixture();await f.migration.admit();await appendDynamic(f);", "const f=await fixture();await appendDynamic(f,oldRuntime);const oldBytes=await readFile(f.source);await f.migration.admit();assert.deepEqual(await readFile(f.source),oldBytes);")
p.write_text(s,encoding='utf-8')
