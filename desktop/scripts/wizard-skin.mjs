// Apply the brand assets to the existing assisted installer, preserving its staged install and update operations.
import fs from 'node:fs';
import path from 'node:path';

function replaceOnce(text,before,after) {
  if (text.split(before).length !== 2) throw Error('Unexpected installer template: ' + before.slice(0,80));
  return text.replace(before, () => after);
}
function read(file) {return fs.readFileSync(file,'utf8').replaceAll('\r\n','\n');}

export function prepareWizardSkin(builderRequire, setup, baseInclude, assets = path.join(import.meta.dirname,'../assets/installer')) {
  const builderNsis = builderRequire('app-builder-lib/out/targets/nsis/nsisUtil.js');
  const templates = builderNsis.nsisTemplatesDir;
  if (path.dirname(path.resolve(templates)) !== path.resolve(setup)) throw Error('The base template must be staged before applying the wizard');
  // electron-builder passes the asset file paths directly to NSIS. Keep them in the short build path.
  const dest = path.join(templates,'maimi-wizard');
  fs.mkdirSync(dest);
  for (const name of ['rail.bmp','header.bmp']) fs.copyFileSync(path.join(assets,name),path.join(dest,name));
  fs.copyFileSync(path.join(path.dirname(baseInclude),'restart-banner.nsh'),path.join(dest,'restart-banner.nsh'));
  fs.copyFileSync(path.join(import.meta.dirname,'wizard-layout.nsh'),path.join(dest,'wizard-layout.nsh'));
  let include = read(baseInclude);
  include=replaceOnce(include,'BrandingText "麦米办公助手"','BrandingText " "');
  include=replaceOnce(include,'!define MUI_BGCOLOR "F3F7F9"','!define MUI_BGCOLOR "F7FAF9"');
  include=replaceOnce(include,'!define MUI_TEXTCOLOR "193A50"','!define MUI_TEXTCOLOR "163D44"');
  include=replaceOnce(include,'!define MUI_FINISHPAGE_TITLE "麦米办公助手已安装"','!define MUI_FINISHPAGE_TITLE "麦米办公助手已就绪"');
  include=replaceOnce(include,'!define MUI_FINISHPAGE_TEXT "可以从开始菜单打开麦米办公助手。"',
    '!define MUI_FINISHPAGE_TEXT "已安装到 $INSTDIR。$\\r$\\n$\\r$\\n打开客户端，通过公司账号登录即可进入工作区。"');
  include=replaceOnce(include,'!macro customInstallMode',[
    '!macro customWelcomePage',
    '  !insertmacro skipPageIfUpdated',
    '  !define MUI_WELCOMEPAGE_TITLE "安装麦米办公助手"',
    '  !define MUI_WELCOMEPAGE_TEXT "将麦米安装到这台 Windows 电脑上。$\\r$\\n$\\r$\\n接下来可以选择安装目录。后续版本会沿用该目录。"',
    '  !insertmacro MUI_PAGE_WELCOME',
    '!macroend','', '!macro customInstallMode'
  ].join('\n'));
  const entry=path.join(dest,'installer-ui.nsh');
  fs.writeFileSync(entry,include+'\n!include "${__FILEDIR__}\\wizard-layout.nsh"\n');
  const assisted=path.join(templates,'assistedInstaller.nsh');
  let pages=read(assisted);
  pages=replaceOnce(pages,'!define MUI_PAGE_HEADER_SUBTEXT "请选择麦米办公助手的专用文件夹。"','!define MUI_PAGE_HEADER_SUBTEXT "为当前 Windows 用户安装客户端。"');
  pages=replaceOnce(pages,'!define MUI_DIRECTORYPAGE_TEXT_TOP "可以使用推荐位置，或浏览其他磁盘。首次安装请选择新文件夹或空文件夹。"',
    '!define MUI_DIRECTORYPAGE_TEXT_TOP "保留推荐位置，或点击「浏览」选择专用文件夹。首次安装可选择新建或空文件夹。"\n    !define MUI_DIRECTORYPAGE_TEXT_DESTINATION "客户端安装目录"');
  pages=replaceOnce(pages,'!define MUI_PAGE_HEADER_SUBTEXT "正在展开工作区与 Office 组件。请保持此窗口打开。"','!define MUI_PAGE_HEADER_SUBTEXT "正在准备客户端和办公组件，请保持窗口打开。"');
  pages=replaceOnce(pages,'!insertmacro MUI_PAGE_DIRECTORY','!define MUI_PAGE_CUSTOMFUNCTION_SHOW MaimiDirectoryShow\n    !insertmacro MUI_PAGE_DIRECTORY');
  pages=replaceOnce(pages,'!insertmacro MUI_PAGE_INSTFILES','!define MUI_PAGE_CUSTOMFUNCTION_SHOW MaimiProgressShow\n  !insertmacro MUI_PAGE_INSTFILES');
  fs.writeFileSync(assisted,pages);
  const section=path.join(templates,'installSection.nsh');
  let script=read(section);
  for(const [anchor,message] of [['!insertmacro extractEmbeddedAppPackage','正在展开并校验安装包…'],['!insertmacro uninstallOldVersion SHELL_CONTEXT','正在准备安装位置…']]){
    script=replaceOnce(script,anchor,'${IfNot} ${Silent}\n  ShowWindow $mui.InstFilesPage.Text 5\n  SendMessage $mui.InstFilesPage.Text ${WM_SETTEXT} 0 "STR:'+message+'"\n${EndIf}\n'+anchor);
  }
  fs.writeFileSync(section,script);
  return {include:entry, installerSidebar:path.join(dest,'rail.bmp'), installerHeader:path.join(dest,'header.bmp')};
}

