"""Build on a verified local image ID without interpreting config IDs as registries."""
from pathlib import Path
import argparse,json,re,subprocess,uuid,hashlib

def run(*args):return subprocess.run(args,text=True,capture_output=True,check=True,timeout=600).stdout.strip()
def main():
    p=argparse.ArgumentParser();p.add_argument('--base-image-id',required=True);p.add_argument('--context',required=True,type=Path);p.add_argument('--tag',required=True);args=p.parse_args()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',args.base_image_id):raise ValueError('PINNED_CONFIG_ID_REQUIRED')
    if not re.fullmatch(r'maimi-native021-worker:[a-z0-9][a-z0-9._-]{0,80}',args.tag):raise ValueError('CANDIDATE_TAG_REQUIRED')
    if run('docker','context','inspect','--format','{{json .Endpoints.docker.Host}}')!='"unix:///var/run/docker.sock"':raise ValueError('LOCAL_DOCKER_REQUIRED')
    fmt='{"id":{{json .Id}},"os":{{json .Os}},"arch":{{json .Architecture}}}'
    actual=json.loads(run('docker','image','inspect','--format',fmt,args.base_image_id))
    if actual!={'id':args.base_image_id,'os':'linux','arch':'amd64'}:raise ValueError('LOCAL_BASE_MISMATCH')
    archive=args.context/'runtime-native021.tar.gz'
    package=json.loads(archive.with_name(archive.name+'.json').read_text(encoding='utf-8'))
    if package.get('schema')!='maimi-native021-runtime-archive-v1' or package.get('runtime')!='0.2.1-rc.2' or not re.fullmatch('[a-f0-9]{64}',package.get('manifest_sha256','')):raise ValueError('ARCHIVE_RECEIPT_REQUIRED')
    with archive.open('rb') as source:archive_hash=hashlib.file_digest(source,'sha256').hexdigest()
    if archive_hash!=package.get('sha256') or archive.stat().st_size!=package.get('bytes'):raise ValueError('ARCHIVE_CHANGED')
    # BuildKit treats a bare sha256 config ID as a registry image name. A unique
    # local alias carries the already verified object into FROM without a pull.
    alias='maimi-r65-build-base:'+uuid.uuid4().hex
    run('docker','tag',args.base_image_id,alias)
    try:
        if json.loads(run('docker','image','inspect','--format',fmt,alias))!=actual:raise ValueError('BASE_ALIAS_CHANGED')
        run('docker','build','--pull=false','--network=none','--build-arg','CORE_BASE_IMAGE='+alias,'--build-arg','RUNTIME_MANIFEST_SHA256='+package['manifest_sha256'],'-t',args.tag,str(args.context.resolve()))
        if json.loads(run('docker','image','inspect','--format',fmt,alias))!=actual:raise ValueError('BASE_ALIAS_CHANGED_DURING_BUILD')
        result=json.loads(run('docker','image','inspect','--format',fmt,args.tag))
        receipt={'schema':'maimi-native021-local-image-build-v1','status':'BUILT_NOT_DEPLOYED','base':actual,'image':result,'tag':args.tag,'archive_sha256':archive_hash,'manifest_sha256':package['manifest_sha256'],'company_changed':False}
        (args.context/'image-build-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8');print(json.dumps(receipt))
    finally:run('docker','image','rm',alias)
if __name__=='__main__':main()
