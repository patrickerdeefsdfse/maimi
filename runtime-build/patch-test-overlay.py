from pathlib import Path
p=Path('output/dsh020-rc2-r41-20261008/runtime/tests/test-integrated-native.py')
s=p.read_text(encoding='utf-8')
a='    overlays={k:v for k,v in overlays.items() if k.startswith("/app/")}\n'
b=a+'    current=WORKSPACE/"output/dsh020-rc2-r41-20261008/cloud/public"\n    overlays["/app/app/project_platform.py"]=current/"project_platform.py"\n    for source in (current/"maimi_projects").glob("*.py"):overlays["/app/maimi_projects/"+source.name]=source\n'
assert s.count(a)==1
p.write_text(s.replace(a,b),encoding='utf-8')
