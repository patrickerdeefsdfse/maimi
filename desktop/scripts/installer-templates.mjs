import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';

function prepareExtractor(templates) {
  const pins = JSON.parse(fs.readFileSync(path.join(import.meta.dirname, '../deps/pins.json'))).installerExtractor;
  const archive = path.resolve(process.env.MAIMI_SEVEN_EXTRA ?? path.join(process.env.MAIMI_WORK, 'source-assets', pins.extra.name));
  const source = path.resolve(process.env.MAIMI_SEVEN_SOURCE ?? path.join(process.env.MAIMI_WORK, 'source-assets', pins.source.name));
  function verify(file, expected) {
    if (createHash('sha256').update(fs.readFileSync(file)).digest('hex') !== expected)
      throw Error('7-Zip installation input differs from its release pin: ' + path.basename(file));
  }
  verify(archive, pins.extra.sha256);
  verify(source, pins.source.sha256);
  const dest = path.join(templates, '7zip');
  fs.mkdirSync(dest);
  const local7z = process.env.MAIMI_7Z ?? path.join(process.env.ProgramFiles, '7-Zip', '7z.exe');
  execFileSync(local7z, ['x', archive, 'x64/7za.exe', 'License.txt', '-o' + dest, '-y', '-bso0', '-bsp0'], {windowsHide: true});
  const exe = path.join(dest, 'x64', '7za.exe');
  const license = path.join(dest, 'License.txt');
  verify(exe, pins.x64Sha256);
  verify(license, pins.licenseSha256);
  return {exe, license, source};
}

function nsisPath(value) {
  return value.replaceAll('$', () => '$$').replaceAll('"', () => '$\\"');
}

function replacePinned(file, hash, anchor, updated) {
  const original = fs.readFileSync(file, 'utf8').replaceAll('\r\n', '\n');
  if (createHash('sha256').update(original).digest('hex') !== hash || original.split(anchor).length !== 2)
    throw Error('Review changed NSIS template: ' + path.basename(file));
  fs.writeFileSync(file, original.replace(anchor, () => updated));
}

// The pinned template retains the upstream uninstall and registration logic.
export function prepareInstallerTemplates(builderRequire, work) {
  const nsis = builderRequire('app-builder-lib/out/targets/nsis/nsisUtil.js');
  const templates = fs.mkdtempSync(path.join(work, 'nsis-'));
  fs.cpSync(nsis.nsisTemplatesDir, templates, {recursive: true});
  const file = path.join(templates, 'include/extractAppPackage.nsh');
  const source = fs.readFileSync(file, 'utf8').replaceAll('\r\n', '\n');
  const expected = 'E4174388A0F7A1DF0B85A0742AA1EA7A4B2B18F9F29DCCD6EF10A66212F68148';
  const hash = createHash('sha256').update(source).digest('hex').toUpperCase();
  if (hash !== expected) throw Error('Review changed NSIS extraction template before building');
  const tools = prepareExtractor(templates);
  const start = source.indexOf('  Push $OUTDIR');
  const end = source.indexOf('  DoneExtract7za:', start);
  const replacement = fs.readFileSync(path.join(import.meta.dirname, 'install-files.nsh'), 'utf8');
  const staged = fs.readFileSync(path.join(import.meta.dirname, 'stage-installer-files.nsh'), 'utf8')
    .replace('@SEVEN_EXE@', () => nsisPath(tools.exe));
  const commit = replacement
    .replace('@SEVEN_LICENSE@', () => nsisPath(tools.license))
    .replace('@SEVEN_SOURCE@', () => nsisPath(tools.source));
  if (start < 0 || end < 0) throw Error('Missing archive stage in pinned NSIS template');
  const tail = source.slice(end + '  DoneExtract7za:'.length);
  fs.writeFileSync(file, source.slice(0, start) + staged + '\n!macroend\n\n!macro MaimiCommitFiles\n' + commit + tail);
  const extract = '!insertmacro extractEmbeddedAppPackage';
  const uninstall = '!insertmacro uninstallOldVersion SHELL_CONTEXT';
  replacePinned(path.join(templates, 'installSection.nsh'), '74c4dbb4163cdd90ca4850b75d304e916235c882d6af7b5cdad63bffbc977072', uninstall, 'Call MaimiValidateDestination\nIfErrors 0 +3\n  SetErrorLevel 2\n  Quit\n' + extract + '\n\n' + uninstall);
  replacePinned(path.join(templates, 'include/installer.nsh'), '0e319437dd01dcbf911f3f48f664fde0cefbaef704f1cdb1739f63d563f5d4a0', extract, '!insertmacro MaimiCommitFiles');
  const progressPage = '!insertmacro MUI_PAGE_INSTFILES';
  const headers = '!define MUI_PAGE_HEADER_TEXT "正在安装麦米办公助手"\n  !define MUI_PAGE_HEADER_SUBTEXT "正在展开工作区与 Office 组件。请保持此窗口打开。"\n  ';
  const assisted = path.join(templates, 'assistedInstaller.nsh');
  replacePinned(assisted, '8aa1230e9717b428664d613fcc5c6a060883410b34cb7853b517fe11b4ee0a76', progressPage, headers + progressPage);
  const pages = fs.readFileSync(assisted, 'utf8');
  const directoryStart = pages.indexOf('    !include StrContains.nsh');
  const directoryEnd = pages.indexOf('    FunctionEnd', directoryStart);
  if (directoryStart < 0 || directoryEnd < 0) throw Error('Review changed directory page');
  fs.writeFileSync(assisted, pages.slice(0, directoryStart) + [
    '    !insertmacro skipPageIfUpdated',
    '    !define MUI_PAGE_HEADER_TEXT "选择安装位置"',
    '    !define MUI_PAGE_HEADER_SUBTEXT "请选择麦米办公助手的专用文件夹。"',
    '    !define MUI_DIRECTORYPAGE_TEXT_TOP "可以使用推荐位置，或浏览其他磁盘。首次安装请选择新文件夹或空文件夹。"',
    '    !insertmacro MUI_PAGE_DIRECTORY',
  ].join('\n') + pages.slice(directoryEnd + '    FunctionEnd'.length));
  const util = path.join(templates, 'include/installUtil.nsh');
  const previous = fs.readFileSync(util,'utf8').replaceAll('\r\n','\n');
  if (createHash('sha256').update(previous).digest('hex') !== '97bd546b5cd2aaf16b77bc9e2be8a18962dd74ab5c4d23b35b163ca89bf4dd2a')
    throw Error('Review changed legacy uninstaller handoff before building');
  const helpers = fs.readFileSync(path.join(import.meta.dirname,'legacy-paths.nsh'),'utf8');
  const updated = previous.replace('Function uninstallOldVersion', helpers + '\nFunction uninstallOldVersion')
    .replace('  StrCpy $uninstallerFileNameTemp', '  StrCpy $maimiPath $installationDir\n  Call MaimiExtendedPath\n  StrCpy $maimiLegacyDirectory $maimiPath\n  StrCpy $uninstallerFileNameTemp')
    .replaceAll('_?=$installationDir', '_?=$maimiLegacyDirectory')
    .replace('  Push "${ROOT_KEY}"\n  Call uninstallOldVersion\n!macroend','  !insertmacro MaimiRunOldUninstall ${ROOT_KEY}\n!macroend')
    .replace('"$(uninstallFailed): $R0"', '"$(uninstallFailed): $R0" /SD IDOK');
  fs.writeFileSync(util,updated);
  // Restore a previous interrupted handoff before the install mode resolves
  // INSTDIR. This desktop product uses a per-user install.
  const installer = path.join(templates,'installer.nsi');
  const program = fs.readFileSync(installer,'utf8');
  fs.writeFileSync(installer, program.replace('    !insertmacro initMultiUser',
    '    Call MaimiRecoverPerUserLocation\n    !insertmacro initMultiUser'));
  fs.appendFileSync(util,'\nFunction MaimiRecoverPerUserLocation\n  !insertmacro MaimiRecoverLocation HKEY_CURRENT_USER\nFunctionEnd\n');
  nsis.nsisTemplatesDir = templates;
}
