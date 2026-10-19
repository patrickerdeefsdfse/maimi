"""Patch only the separately staged official desktop source, recording every diff."""
from pathlib import Path
import hashlib,json,shutil,difflib,argparse
HERE=Path(__file__).resolve().parents[1]
SOURCE=HERE.parent/'upstream/source/deepseek-harness-687f282e8625c6b8d11ff58211a90524be2e4317'
parser=argparse.ArgumentParser()
parser.add_argument('--stage-root',type=Path,default=HERE/'native-source')
parser.add_argument('--receipt-directory',type=Path,default=HERE)
args=parser.parse_args()
STAGE=args.stage_root.resolve()
RECEIPTS=args.receipt_directory.resolve()
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
 text=one(text,"import { DesktopHostFatalError, DesktopHostProcess, DesktopHostUncleanExitError } from './host-process.ts'","import { DesktopHostFatalError, DesktopHostProcess, DesktopHostUncleanExitError } from './host-process.ts'\nimport { CompanyNativeDesktopHost, COMPANY_CANDIDATE } from './company-shell-host.ts'\nimport { CompanySignOutController } from './company-signout.ts'")
 text=one(text,'app.setAppLogsPath()',"if (COMPANY_CANDIDATE) {\n  app.setName('麦米 DSH 升级候选')\n  app.setPath('userData', join(app.getPath('appData'), 'MegmeetMaimiDSH-r65-candidate'))\n  process.env.DSH_HOME = join(app.getPath('userData'), 'native-shell-state')\n}\napp.setAppLogsPath()")
 text=one(text,"    const hostInspectPort = developmentHostInspectPort(development)","""    if (COMPANY_CANDIDATE) {
      const host = new CompanyNativeDesktopHost(resources.dsh, onFailure, primaryRuntime, resources)
      companyActiveHost = host
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
 text=one(text,'  let requireCleanStop = false',"  let companyActiveHost: CompanyNativeDesktopHost | undefined\n  let requestCompanySignOut: () => Promise<boolean> = () => Promise.resolve(false)\n  let requireCleanStop = false")
 text=one(text,"  const applicationItems = (): MenuItemConstructorOptions[] => [","""  const applicationItems = (): MenuItemConstructorOptions[] => [
    ...COMPANY_CANDIDATE ? [{
      label: '退出公司登录并关闭麦米',
      enabled: backend.host !== undefined && !quitting && !shellInstallerOwnsQuit,
      click: () => { void requestCompanySignOut() },
    }, { type: 'separator' as const }] : [],""")
 text=one(text,"  if (process.platform === 'win32') {\n    ipcMain.handle(DESKTOP_IPC.windowsMenu,", """  const companySignOut = new CompanySignOutController({
    current: () => {
      const company = companyActiveHost
      const host = backend.host
      if (!COMPANY_CANDIDATE || company === undefined || host === undefined || quitting || shellInstallerOwnsQuit) return undefined
      return {
        identity: company,
        lock: () => host.updateTasks('lock'),
        unlock: () => host.updateTasks('unlock'),
        blockResume: () => company.blockResume(),
        stop: async () => { try { await platformView.closeAndWait() } finally { await backend.close(); companyActiveHost = undefined } },
        revoke: () => company.signOut(),
      }
    },
    confirmAccount: async () => (await dialog.showMessageBox({
      type: 'question', title: '退出公司登录', message: '退出公司登录并关闭麦米？',
      detail: '此电脑下次打开麦米需要重新登录。当前本地聊天和文件会保留。',
      buttons: ['退出登录', '取消'], defaultId: 1, cancelId: 1, noLink: true,
    })).response === 0,
    confirmTasks: () => quitConfirmation.confirm(),
    notify: async (result) => { await dialog.showMessageBox({
      type: 'warning', title: '麦米公司登录',
      message: result === 'revocation-pending' ? '本机已退出，服务器撤销将在网络恢复后的下次启动时重试。'
        : result === 'stop-failed' ? '已阻止本机自动登录，任务退出未能完整确认，麦米将关闭。'
          : '本次退出未能完成，请稍后重试。',
      buttons: ['知道了'], noLink: true,
    }) },
    finish: () => { quitWithoutConfirmation() },
  })
  requestCompanySignOut = () => companySignOut.run()

  if (process.platform === 'win32') {
    ipcMain.handle(DESKTOP_IPC.windowsMenu,""")
 text=one(text,'        await Promise.all([manager.applyRelease(), prepareHostEnvironment()])','        await Promise.all([COMPANY_CANDIDATE ? Promise.resolve() : manager.applyRelease(), prepareHostEnvironment()])')
 text=one(text,'    undefined, undefined, undefined,','    undefined, COMPANY_CANDIDATE ? () => false : undefined, undefined,')
 text=one(text,"    if (welcomeBackend === undefined) throw new Error('desktop welcome: backend unavailable')\n    return { languages: systemLanguages, preference: await welcomeBackend.readLocalePreference() }","    if (COMPANY_CANDIDATE) return { languages: systemLanguages, preference: 'zh-CN' }\n    if (welcomeBackend === undefined) throw new Error('desktop welcome: backend unavailable')\n    return { languages: systemLanguages, preference: await welcomeBackend.readLocalePreference() }")
 text=one(text,'    return (await readWelcomeState()).hasApiKey','    return COMPANY_CANDIDATE ? backend.host !== undefined : (await readWelcomeState()).hasApiKey')
 text=one(text,'  const automaticCheck = (): void => {','  const automaticCheck = (): void => {\n    if (COMPANY_CANDIDATE) return')
 text=one(text,"  const policyConfig = resolveDesktopPolicyConfig(policyInput, !app.isPackaged)","  const policyConfig = resolveDesktopPolicyConfig(COMPANY_CANDIDATE ? undefined : policyInput, !app.isPackaged)")
 text=one(text,'  const showWelcome = (): Promise<void> => {','  const showWelcome = (): Promise<void> => {\n    if (COMPANY_CANDIDATE) return reconcileBackend()')
 # Keep upstream's required DSH_DESKTOP_PRIMARY_RUNTIME_DIR. The official
 # development launcher sets it through developmentRuntimeDirectory(), including
 # the target directory and custom build root. Guessing .desktop-build/primary-runtime
 # bypasses that contract and points at a nonexistent payload.
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
shutil.copyfile(HERE/'src/company-signout.ts',app/'src/company-signout.ts')
own=app/'maimi';own.mkdir(exist_ok=True)
for p in (HERE/'src').rglob('*'):
 if p.is_file() and p.name not in ('company-shell-host.ts','company-signout.ts'):
  to=own/p.relative_to(HERE/'src');to.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,to)
(RECEIPTS/'patches').mkdir(parents=True,exist_ok=True)
(RECEIPTS/'patches/native-shell.patch').write_text(''.join(diff),encoding='utf-8',newline='\n')
receipt={'schema':'maimi-native-shell-integration-r65-v1','status':'SOURCE_CANDIDATE_PREPARED','default_work_mode':'NATIVE_LOCAL','company_login':'EXISTING_IAM_PKCE','official_update_feed_enabled':False,'official_account_ui_enabled':False,'cloud_entry':'NATIVE_SIDEBAR_FOOTER_BROWSER_LINK','official_client_bundles_modified':0,'changed_upstream_files':changes,'electron_shell_build':'NOT_YET_TESTED','company_requests':0,'services_changed':False}
(RECEIPTS/'shell-integration.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':receipt['status'],'changed_upstream_files':len(changes),'official_client_bundles_modified':0,'company_requests':0}))
