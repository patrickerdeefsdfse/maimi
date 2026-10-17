from pathlib import Path
p=Path('/home/megmeet/dsh020-r41/runtime-linux-x64/node_modules/@deepseek-ai/dsh-llm/lib/index.js');ls=p.read_text().splitlines()
for i,l in enumerate(ls):
 if 'listProviders(' in l or 'listModels(' in l:print(i+1,'\n'.join(ls[i:i+14]))
