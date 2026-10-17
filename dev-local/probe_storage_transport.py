"""Read-only operation on the known all-f synthetic local rehearsal owner."""
import os,pathlib,httpx
key=pathlib.Path(os.environ['PERSONAL_CLOUD_MANAGER_SECRET_FILE']).read_text().strip()
r=httpx.post(os.environ['PERSONAL_CLOUD_MANAGER_URL']+'/v1/storage/'+'f'*32,
 headers={'Authorization':'Bearer '+key},json={'action':'inventory'},timeout=110,trust_env=False)
print('HTTP_STATUS:',r.status_code)
print('CHECK:',r.json().get('detail','completed'))
