from pathlib import Path
p=Path('output/dsh020-rc2-r41-20261008/runtime/assemble-linux.py');s=p.read_text(encoding='utf-8')
s=s.replace("TREE=TASK/'build/tree'\nTREE.mkdir", "TREE=TASK/'build/tree'\nassert TREE.resolve()==Path('/home/megmeet/dsh020-r41/build/tree')\nif TREE.exists():shutil.rmtree(TREE)\nTREE.mkdir")
s=s.replace("shutil.copytree(TASK/'base-export/personal-cloud',P,dirs_exist_ok=True,symlinks=True)","shutil.copytree(TASK/'base-export/personal-cloud',P,dirs_exist_ok=True,symlinks=True)\nfor obsolete in ('candidate-manifest.json','rc2-source-pins.json','guard-runtime.mjs','verify-existing-runtime.mjs','manager.py'):\n if (P/obsolete).exists():(P/obsolete).unlink()")
p.write_text(s,encoding='utf-8')
