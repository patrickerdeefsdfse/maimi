from pathlib import Path
base=Path('/home/megmeet/dsh020-r41/runtime-linux-x64/node_modules/@deepseek-ai')
p=base/'dsh-client-connection/lib/index.js';s=p.read_text();i=s.find('async function initializeSecret');print('INIT_AUTH',s[i:i+2900]);i=s.rfind('async function apply');print('CONNECTION_APPLY',s[i:i+2500]);print('CONNECTION_END',s[-3000:])
p=base/'dsh-credentials-local/lib/index.js';s=p.read_text();print('CRED_TAIL',s[-15000:])
