from pathlib import Path
import json, shutil
root=Path(__file__).resolve().parent
repo=root.parents[2]
old=repo/'output/dsh-rc2-implementation-20260928/ui'
s=(old/'test-workflow-browser.cjs').read_text(encoding='utf-8')
s=s.replace("runtime=here,area=path.join(here,'ui-browser-fixtures')", "runtime="+json.dumps(r'C:\Users\LI YUHAO\.codex\tmp\dsh020-r41-desktop')+",area=path.join(runtime,'ui-browser-fixtures')")
s=s.replace("runtime:'0.1.7-rc.2'", "runtime:'0.2.0-rc.2'")
s=s.replace("[{id:'llm-deepseek',disabled:true},{id:'llm-deepseek-account',disabled:true},{id:'session-log-deepseek',config:{enabled:false}},{id:'session-telemetry-otel',config:{mode:'DISABLED'}},{id:'skill-office',config:{cli:false}}]", "(await import(require('node:url').pathToFileURL(path.join(here,'app/enterprise-profile.mjs')))).enterpriseProfile({cloud:false})")
(root/'test-workflow-browser.cjs').write_text(s,encoding='utf-8')
shutil.copyfile(old/'synthetic.xlsx',root/'synthetic.xlsx')
print('DSH020_REAL_BROWSER_HARNESS_PREPARED')
