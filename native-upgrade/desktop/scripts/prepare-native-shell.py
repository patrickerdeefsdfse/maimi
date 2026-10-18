"""Patch only the separately staged official desktop source, recording every diff."""
from pathlib import Path
import hashlib,json,shutil,difflib
HERE=Path(__file__).resolve().parents[1]
SOURCE=HERE.parent/'upstream/source/deepseek-harness-687f282e8625c6b8d11ff58211a90524be2e4317'
STAGE=HERE/'native-source'
lock=json.loads((HERE/'upstream-source-lock.json').read_text(encoding='utf-8'))
pins={r['path']:r for r in lock['files']}
if not (STAGE/'.maimi-native-candidate.json').is_file():raise RuntimeError('STAGED_NATIVE_SOURCE_REQUIRED')
changes=[];diff=[]
def edit(relative,transform):
 source=SOURCE/relative;data=source.read_bytes()
 if hashlib.sha256(data).hexdigest()!=pins[relative]['sha256']:raise RuntimeError('UPSTREAM_SOURCE_CHANGED')
 before=data.decode('utf-8');after=transform(before)
 (STAGE/relative).write_text(after,encoding='utf-8',newline='\n')
 changes.append({'path':relative,'upstream_sha256':pins[relative]['sha256'],'candidate_sha256':hashlib.sha256(after.encode()).hexdigest()})
 diff.extend(difflib.unified_diff(before.splitlines(True),after.splitlines(True),'a/'+relative,'b/'+relative))
def one(text,old,new):
 if text.count(old)!=1:raise RuntimeError('NATIVE_INTEGRATION_ANCHOR_CHANGED')
 return text.replace(old,new)
def main(text):
 text=one(text,"import { DesktopHostFatalError, DesktopHostProcess, DesktopHostUncleanExitError } from './host-process.ts'","import { DesktopHostFatalError, DesktopHostProcess, DesktopHostUncleanExitError } from './host-process.ts'\nimport { CompanyNativeDesktopHost, COMPANY_CANDIDATE } from './company-shell-host.ts'")
 text=one(text,'app.setAppLogsPath()',"if (COMPANY_CANDIDATE) {\n  app.setName('麦米 DSH 升级候选')\n  app.setPath('userData', join(app.getPath('appData'), 'MegmeetMaimiDSH-r65-candidate'))\n  process.env.DSH_HOME = join(app.getPath('userData'), 'native-shell-state')\n}\napp.setAppLogsPath()")
 text=one(text,"    const hostInspectPort = developmentHostInspectPort(development)","""    if (COMPANY_CANDIDATE) {
      const host = new CompanyNativeDesktopHost(resources.dsh, onFailure, primaryRuntime, resources)
      return {
        start: async () => {
          const ready = await host.start()
          hostCookie = await authenticateWebHost(ready.url)
          hostUrl = ready.url
          if (ready.injections === undefined) throw new Error('Company Host did not provide native boot injections')
          injections = ready.injections
          analyticsEnabled = false
        },
        stop: () => host.stop(requireCleanStop),
        updateTasks: (action: 'inspect' | 'lock' | 'unlock') => host.updateTasks(action),
        inspectQuit: () => host.inspectQuit(),
      }
    }
    const hostInspectPort = developmentHostInspectPort(development)""")
 text=one(text,'  const readWelcomeState = async () => {',"  const readWelcomeState = async () => {\n    if (COMPANY_CANDIDATE) return { loggedIn: backend.host !== undefined, hasApiKey: false, writable: false, localePreference: 'zh-CN' }")
 text=one(text,'        await Promise.all([manager.applyRelease(), prepareHostEnvironment()])','        await Promise.all([COMPANY_CANDIDATE ? Promise.resolve() : manager.applyRelease(), prepareHostEnvironment()])')
 text=one(text,'    undefined, undefined, undefined,','    undefined, COMPANY_CANDIDATE ? () => false : undefined, undefined,')
 text=one(text,"    if (welcomeBackend === undefined) throw new Error('desktop welcome: backend unavailable')\n    return { languages: systemLanguages, preference: await welcomeBackend.readLocalePreference() }","    if (COMPANY_CANDIDATE) return { languages: systemLanguages, preference: 'zh-CN' }\n    if (welcomeBackend === undefined) throw new Error('desktop welcome: backend unavailable')\n    return { languages: systemLanguages, preference: await welcomeBackend.readLocalePreference() }")
 text=one(text,'    return (await readWelcomeState()).hasApiKey','    return COMPANY_CANDIDATE ? backend.host !== undefined : (await readWelcomeState()).hasApiKey')
 text=one(text,'  const automaticCheck = (): void => {','  const automaticCheck = (): void => {\n    if (COMPANY_CANDIDATE) return')
 text=one(text,"  const policyConfig = resolveDesktopPolicyConfig(policyInput, !app.isPackaged)","  const policyConfig = resolveDesktopPolicyConfig(COMPANY_CANDIDATE ? undefined : policyInput, !app.isPackaged)")
 text=one(text,'  const showWelcome = (): Promise<void> => {','  const showWelcome = (): Promise<void> => {\n    if (COMPANY_CANDIDATE) return reconcileBackend()')
 text=one(text,"    ? developmentPrimaryRuntime()","    ? (COMPANY_CANDIDATE ? (process.env.DSH_DESKTOP_PRIMARY_RUNTIME_DIR ?? join(app.getAppPath(), '.desktop-build', 'primary-runtime')) : developmentPrimaryRuntime())")
 text=one(text,'    return platformView.open(owner, page, platformBounds(bounds))',"    if (COMPANY_CANDIDATE) throw new Error('COMPANY_ACCOUNT_MANAGED_BY_IAM')\n    return platformView.open(owner, page, platformBounds(bounds))")
 return text
edit('apps/desktop/src/main.ts',main)
def builder(text):
 text=one(text,'  const appId = resolveDesktopAppId(env)',"  const appId = 'com.megmeet.maimi.r65candidate'")
 text=one(text,"    productName: 'DeepSeek Harness',","    productName: '麦米 DSH 升级候选',")
 text=one(text,"      'lib/main.js',","      'lib/main.js',\n      'maimi/**/*',")
 return text
edit('apps/desktop/scripts/electron-builder-config.mjs',builder)
edit('apps/desktop-host/src/index.ts',lambda text:one(text,"import * as desktopOffice from './office.ts'","import * as desktopOffice from './office.ts'\n// Expose the unchanged native Office composition to the company Host adapter.\nexport { desktopOffice }\nexport { installOfficeEngineResolution } from './office-engine.ts'"))
app=STAGE/'apps/desktop'
shutil.copyfile(HERE/'src/company-shell-host.ts',app/'src/company-shell-host.ts')
own=app/'maimi';own.mkdir(exist_ok=True)
for p in (HERE/'src').rglob('*'):
 if p.is_file() and p.name!='company-shell-host.ts':
  to=own/p.relative_to(HERE/'src');to.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,to)
(HERE/'patches').mkdir(exist_ok=True)
(HERE/'patches/native-shell.patch').write_text(''.join(diff),encoding='utf-8',newline='\n')
receipt={'schema':'maimi-native-shell-integration-r65-v1','status':'SOURCE_CANDIDATE_PREPARED','default_work_mode':'NATIVE_LOCAL','company_login':'EXISTING_IAM_PKCE','official_update_feed_enabled':False,'official_account_ui_enabled':False,'cloud_entry':'NATIVE_SIDEBAR_FOOTER_BROWSER_LINK','official_client_bundles_modified':0,'changed_upstream_files':changes,'electron_shell_build':'NOT_YET_TESTED','company_requests':0,'services_changed':False}
(HERE/'shell-integration.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':receipt['status'],'changed_upstream_files':len(changes),'official_client_bundles_modified':0,'company_requests':0}))
