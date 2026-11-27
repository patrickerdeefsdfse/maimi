BrandingText "麦米办公助手"
!define MUI_INSTFILESPAGE_TITLE "正在安装麦米办公助手"
!define MUI_INSTFILESPAGE_TEXT "正在展开工作区与 Office 组件。请保持此窗口打开。"
!define MUI_FINISHPAGE_TITLE "麦米办公助手已安装"
!define MUI_FINISHPAGE_TEXT "可以从开始菜单打开麦米办公助手。"
!define MUI_FINISHPAGE_RUN_TEXT "现在打开麦米办公助手"
!define MUI_BGCOLOR "F3F7F9"
!define MUI_TEXTCOLOR "193A50"
!define MUI_HEADER_TRANSPARENT_TEXT
!macro customInstallMode
  StrCpy $isForceCurrentInstall "1"
!macroend

!ifndef BUILD_UNINSTALLER
  Var InstallerTheme
  !include "${__FILEDIR__}\restart-banner.nsh"
  ; The uninstaller owns this whole directory. Reuse the registered location,
  ; otherwise require a dedicated new or empty directory.
  !define /ifndef INSTALL_REGISTRY_KEY "Software\${APP_GUID}"
  Function MaimiValidateDestination
    Push $0
    StrCmp $INSTDIR "" destinationRejected
    System::Call 'shlwapi::PathIsRootW(w "$INSTDIR") i .r0'
    StrCmp $0 0 0 destinationRejected
    ReadRegStr $0 HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation
    StrCmp $INSTDIR $0 destinationAccepted
    System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR") i .r0'
    StrCmp $0 -1 destinationAccepted
    IntOp $0 $0 & 0x400
    StrCmp $0 0 0 destinationRejected
    System::Call 'shlwapi::PathIsDirectoryEmptyW(w "$INSTDIR") i .r0'
    StrCmp $0 0 destinationRejected
    destinationAccepted:
      Pop $0
      ClearErrors
      Return
    destinationRejected:
      Pop $0
      SetErrors
  FunctionEnd

  Function .onVerifyInstDir
    Call MaimiValidateDestination
    IfErrors 0 +2
      Abort
  FunctionEnd
!endif

!macro customInit
  Push $0
  ReadRegDWORD $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Themes\Personalize" "AppsUseLightTheme"
  StrCpy $InstallerTheme "light"
  ${If} $0 == "0"
    StrCpy $InstallerTheme "dark"
  ${EndIf}
  Pop $0
  ClearErrors
  ${If} ${Silent}
  ${AndIf} ${isUpdated}
  ${AndIf} ${isForceRun}
    InitPluginsDir
    File "/oname=$PLUGINSDIR\window-frame.dll" "$%MAIMI_WORK%\installer-ui\window-frame.dll"
    !insertmacro ShowRestartBanner
  ${EndIf}
!macroend

!macro customInstall
  ${If} $RestartStatusJob != ""
  ${AndIf} $RestartStatusJob != 0
    System::Call 'kernel32::CloseHandle(p $RestartStatusJob)'
    StrCpy $RestartStatusJob 0
  ${EndIf}
!macroend
