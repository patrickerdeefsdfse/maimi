from pathlib import Path
p=Path('output/dsh020-rc2-r41-20261008/runtime/tests/test-history-continuation.mjs');s=p.read_text(encoding='utf-8')
s=s.replace('return {state,workspace,sessions,id,source,sourceHash:', 'return {state,workspace,sessions,id,source,compression,sourceHash:')
s=s.replace('backend(root,f.sessions)', 'backend(root,f.sessions,f.compression)')
p.write_text(s,encoding='utf-8')
