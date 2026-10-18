"""Assemble an isolated 0.2.1 runtime without overwriting official package code."""
from pathlib import Path
import argparse, hashlib, json, shutil, os

VERSION='0.2.1-rc.2'
HERE=Path(__file__).resolve().parent

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def module_inventory(directory):
    """Hash every installed file and preserve internal links without following them."""
    result={}
    for current, directories, files in os.walk(directory, followlinks=False):
        for name in sorted(directories+files):
            p=Path(current)/name
            rel=p.relative_to(directory).as_posix()
            if p.is_symlink():
                if not p.resolve(strict=True).is_relative_to(directory.resolve()):
                    raise ValueError('EXTERNAL_MODULE_LINK')
                result[rel]={'kind':'symlink','target':os.readlink(p)}
            elif p.is_file():result[rel]={'kind':'file','sha256':sha(p)}
            elif not p.is_dir():raise ValueError('SPECIAL_MODULE_FILE')
    return result

def assemble(runtime: Path, project: Path, destination: Path, reference_modules=False):
    runtime=runtime.resolve(); project=project.resolve(); destination=destination.resolve()
    if destination.exists(): raise ValueError('DESTINATION_MUST_NOT_EXIST')
    pkg=json.loads((runtime/'node_modules/@deepseek-ai/dsh/package.json').read_text(encoding='utf-8'))
    if pkg['version']!=VERSION: raise ValueError('EXACT_UPSTREAM_RUNTIME_REQUIRED')
    modules=runtime/'node_modules'
    inventory={}
    for p in sorted((modules/'@deepseek-ai').glob('*/package.json')):
        row=json.loads(p.read_text(encoding='utf-8')); inventory[row['name']]=row['version']
        if (row['name']=='@deepseek-ai/dsh' or row['name'].startswith('@deepseek-ai/dsh-')) and row['version']!=VERSION:
            raise ValueError('MIXED_DSH_RELEASE')
    d=destination/'opt/dsh'; own=destination/'opt/personal-cloud'
    d.mkdir(parents=True); own.mkdir(parents=True)
    source_inventory=module_inventory(modules)
    if reference_modules:(d/'node_modules').mkdir()
    else:
        shutil.copytree(modules,d/'node_modules',symlinks=True)
        if module_inventory(d/'node_modules')!=source_inventory:raise ValueError('MODULE_COPY_CHANGED')
    for name in ('package.json','package-lock.json'):shutil.copyfile(runtime/name,d/name)
    overlay=project/'runtime-overlays/worker/opt'
    for p in (overlay/'personal-cloud').iterdir():
        if p.is_file() and p.name not in {'runtime020-source-pins.json','verify-runtime020.mjs'}:shutil.copyfile(p,own/p.name)
    for p in (overlay/'dsh').iterdir():
        if p.name=='node_modules':continue
        if p.is_dir():shutil.copytree(p,d/p.name)
        else:shutil.copyfile(p,d/p.name)
    # These two company registrations were exercised with the new Skill service.
    # Update our peer contract; do not grant upstream version exemptions.
    for name in ('review-skills','logistics-skill'):
        file=d/name/'package.json'
        package=json.loads(file.read_text(encoding='utf-8'))
        if package.get('peerDependencies',{}).get('@deepseek-ai/dsh-skill') not in {'0.2.0-rc.2',VERSION}:
            raise ValueError('COMPANY_SKILL_CONTRACT_CHANGED')
        package['peerDependencies']['@deepseek-ai/dsh-skill']=VERSION
        file.write_text(json.dumps(package,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for name in ('enterprise-profile.mjs','community-profile.mjs','company-workspace-controller.mjs'):
        shutil.copyfile(HERE/name,own/name)
    # UI is now an independent company plugin. Never replace a shipped DSH bundle.
    ui=d/'plugins/company-ui';ui.mkdir(parents=True)
    source=project/'experimental/service-entry-r63/cloud-brand-client.js'
    text=source.read_text(encoding='utf-8')
    anchor='window.__ModuleLoader__.load({'
    if text.count(anchor)!=1:raise ValueError('COMPANY_UI_ANCHOR_CHANGED')
    # The old transport recovery policy is retired; upstream owns reconnection.
    text=text[text.index(anchor):]
    first='  // r47 reconnects the native transport, retaining the live input/controller.'
    last="  ctx.inject(['connection', 'workspaces', 'uiWorkspace']"
    if text.count(first)!=1 or text.count(last)!=1:raise ValueError('COMPANY_UI_RECOVERY_ANCHOR_CHANGED')
    start=text.index(first);end=text.index(last,start)
    text=text[:start]+text[end:]
    text=text.replace(' attachCloudRecovery();','')
    old='id: "@deepseek-ai/dsh-client-ui-brand-official"'
    if text.count(old)!=1:raise ValueError('COMPANY_UI_MODULE_ANCHOR_CHANGED')
    text=text.replace(old,'id: "@megmeet/dsh-company-ui"')
    (ui/'client.js').write_text(text,encoding='utf-8',newline='\n')
    (ui/'index.mjs').write_text('export function apply() {}\n',encoding='utf-8')
    (ui/'package.json').write_text(json.dumps({'name':'@megmeet/dsh-company-ui','version':'0.1.0','private':True,'type':'module','main':'index.mjs','exports':{'.':'./index.mjs','./client':'./client.js','./package.json':'./package.json'},'engines':{'dsh':VERSION},'dsh':{'client':{'platform':'web','inject':['@deepseek-ai/dsh-client-ui-renderer','@deepseek-ai/dsh-client-ui-sidebar']}}},indent=2)+'\n',encoding='utf-8')
    targets={}
    for root in (own,d):
        for p in root.rglob('*'):
            if p.is_file() and 'node_modules' not in p.relative_to(destination).parts:
                targets['/'+p.relative_to(destination).as_posix()]=sha(p)
    manifest={'schema':'maimi-native021-runtime-candidate-v1','runtime':VERSION,'official_packages_modified':0,'packages':inventory,'module_files':source_inventory,'source_package_lock_sha256':sha(runtime/'package-lock.json'),'company_source_targets':targets,'company_ui_source_sha256':sha(source),'deployment_status':'NOT_DEPLOYED','acceptance':'PENDING','modules_mode':'LOCAL_TEST_EXTERNAL_MOUNT' if reference_modules else 'COPIED_COMPLETE_TREE'}
    (destination/'candidate-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    return manifest

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--runtime',required=True,type=Path);parser.add_argument('--project',required=True,type=Path);parser.add_argument('--destination',required=True,type=Path);parser.add_argument('--reference-modules',action='store_true',help='Local testing only: mount the locked runtime node_modules separately; output is not deployable.')
    args=parser.parse_args(); result=assemble(args.runtime,args.project,args.destination,args.reference_modules)
    print(json.dumps({'status':'ASSEMBLED_CANDIDATE','runtime':result['runtime'],'official_packages_modified':0,'company_source_targets':len(result['company_source_targets']),'company_changed':False}))
