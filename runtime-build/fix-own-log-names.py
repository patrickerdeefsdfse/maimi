from pathlib import Path
r=Path('/mnt/d/麦格米特/linux/output/dsh020-rc2-r41-20261008/runtime')
for p in r.iterdir():
 if p.name.endswith('\r'):
  dest=p.with_name(p.name.rstrip('\r'));assert dest.parent==r and p.is_file()
  if dest.exists():assert dest.read_bytes()==p.read_bytes()
  else:p.rename(dest)
print('OWN_LOG_NAMES_NORMALIZED')
