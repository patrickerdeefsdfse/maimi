from pathlib import Path
import hashlib,json,shutil
R=Path(__file__).resolve().parent;W=R.parents[2]
base=W/'output/dsh-rc2-implementation-20260928/release-r27/bundle-candidate'
m=json.loads((base/'release-manifest.json').read_text())
parent=('worker.mjs','idle-control.mjs','catalog.mjs','vision-control-plugin.mjs','vision-admission.mjs','task-authority.mjs','web-control-plugin.mjs','web-bridge.mjs','managed-profile.mjs','community-profile.mjs')
rows=[]
for name,target in [(n,'opt/personal-cloud/'+n) for n in parent]+[(n,'opt/dsh/toolkit/'+n) for n in ('project-routing.mjs','project-bridge.mjs','session-tools.mjs')]:
 dest=R/'overlay'/target
 if dest.exists():continue # r29/r30/new020 explicit overlay always wins.
 src=base/'public'/name
 b=src.read_bytes();sha=hashlib.sha256(b).hexdigest();assert sha==m['files']['public/'+name]['sha256']
 dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(b)
 rows.append({'source':str(src.relative_to(W)),'target':'/'+target,'sha256':sha,'release':'r27','reason':'inherited_not_replaced_by_r29_or_r30'})
(R/'inherited-overlay-provenance.json').write_text(json.dumps(rows,indent=2)+'\n')
print(json.dumps({'inherited_added':[v['target'] for v in rows]}))
