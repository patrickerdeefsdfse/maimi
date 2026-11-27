!ifndef BUILD_UNINSTALLER
Var RestartStatusJob
!endif

!macro ShowRestartBanner
  Push $0
  ${If} ${Errors}
    StrCpy $0 1
  ${Else}
    StrCpy $0 0
  ${EndIf}
  Push $1
  Push $2
  StrCpy $1 0
  StrCpy $2 "麦米办公助手正在更新，请稍候。安装完成后会自动重新打开。"
  System::Call '$PLUGINSDIR\window-frame.dll::InstallerStartRestartStatus(w "$EXEPATH", w "$InstallerTheme", i $LANGUAGE, w r2) p.r1 ?c'
  StrCpy $RestartStatusJob $1
  Pop $2
  Pop $1
  ${If} $0 == 1
    SetErrors
  ${Else}
    ClearErrors
  ${EndIf}
  Pop $0
!macroend
