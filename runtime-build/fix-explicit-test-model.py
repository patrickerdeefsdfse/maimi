from pathlib import Path
p=Path('output/dsh020-rc2-r41-20261008/runtime/tests/test-history-continuation.mjs')
s=p.read_text(encoding='utf-8')
a="providers:{deepseek:{apiKeyEnv:'RC2_SYNTHETIC_KEY',baseURL:"
b="providers:{deepseek:{api:'openai-completions',models:[{id:'deepseek-v4-flash',name:'Synthetic explicit fixture model'}],apiKeyEnv:'RC2_SYNTHETIC_KEY',baseURL:"
assert s.count(a)==1
p.write_text(s.replace(a,b),encoding='utf-8')
