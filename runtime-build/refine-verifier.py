from pathlib import Path
p=Path('output/dsh020-rc2-r41-20261008/runtime/verify-runtime.mjs');s=p.read_text(encoding='utf-8')
s=s.replace("if(p.runtime!=='0.2.0-rc.2')throw Error('RUNTIME_PIN_INVALID');","if(p.runtime!=='0.2.0-rc.2')throw Error('RUNTIME_PIN_INVALID');\nif(process.version!=='v22.20.0')throw Error('UNREVIEWED_NODE_RUNTIME');")
s=s.replace(" if(pkg.name!==name||pkg.version!==version)throw Error('PACKAGE_VERSION_MISMATCH');"," if(pkg.name!==name||pkg.version!==version)throw Error('PACKAGE_VERSION_MISMATCH');\n if((name==='@deepseek-ai/dsh'||name.startsWith('@deepseek-ai/dsh-'))&&version!==p.runtime)throw Error('MIXED_DSH_RUNTIME');")
p.write_text(s,encoding='utf-8')
p=Path('output/dsh020-rc2-r41-20261008/runtime/assemble-linux.py');s=p.read_text(encoding='utf-8')
s=s.replace("# Pin reviewed hand-written worker/bridge sources and every modified native bundle.\npins={}","# Pin reviewed hand-written worker/bridge sources and every modified native bundle.\nshutil.copyfile(SRC/'verify-runtime.mjs',P/'verify-runtime020.mjs')\npins={}")
s=s.replace("shutil.copyfile(SRC/'verify-runtime.mjs',P/'verify-runtime020.mjs')\nprint", "print")
p.write_text(s,encoding='utf-8')
