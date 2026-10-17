from pathlib import Path
root=Path(__file__).resolve().parent
source=root.parent/'runtime/tests'
(root/'native-tests').mkdir(exist_ok=True)
for name in ('test-history-continuation.mjs','test-history-incremental.mjs'):
 s=(source/name).read_text(encoding='utf-8')
 s="import {tmpdir} from 'node:os';\n"+s
 s=s.replace("'../overlay/opt/personal-cloud/", "'../app/")
 s=s.replace("mkdtemp('/tmp/rc2-history-')", "mkdtemp(path.join(tmpdir(),'win020-history-'))")
 s=s.replace("mkdtemp('/tmp/r29-history-')", "mkdtemp(path.join(tmpdir(),'win020-incremental-'))")
 (root/'native-tests'/name).write_text(s,encoding='utf-8')
print('WINDOWS_REAL_HISTORY_HARNESS_PREPARED')
