!ifndef MEGMEET_MAX_INSTALL_DIR
  !error "Generate the payload path policy before compiling this installer"
!endif
!define MUI_DIRECTORYPAGE_TEXT_TOP "请选择本人可写的安装目录。目录（含自动添加的应用子目录）最多 ${MEGMEET_MAX_INSTALL_DIR} 个字符；过长时请改用较短目录。支持中文和空格。"

; Keep both attended installation and silent updates per-user, without UAC.
!macro customInstallMode
  StrCpy $isForceMachineInstall "0"
  StrCpy $isForceCurrentInstall "1"
!macroend

!macro customInit
  ${If} ${isForAllUsers}
    MessageBox MB_OK|MB_ICONINFORMATION "此安装器仅安装到当前用户，请选择本人有写入权限的文件夹。" /SD IDOK
    SetErrorLevel 3
    Quit
  ${EndIf}
  StrCpy $hasPerMachineInstallation "0"
  StrCpy $hasPerUserInstallation "1"
  !insertmacro setInstallModePerUser
!macroend

; Compare the target executable's complete path, rather than blocking any
; development/portable instance elsewhere which happens to have the same name.
; Query failure for a matching executable fails closed. Never kill a process.
!macro customHeader
  Var megmeetRunningResult
  !ifndef BUILD_UNINSTALLER
    ; NSIS validates this on every directory change and disables Next for an
    ; invalid path. Account for the exact subdirectory added by the pinned
    ; assistedInstaller.nsh before it reaches the file-writing section.
    Function .onVerifyInstDir
      Push $0
      Push $1
      StrCpy $1 $INSTDIR
      ${StrContains} $0 "${APP_FILENAME}" $1
      ${If} $0 == ""
        StrCpy $1 "$1\${APP_FILENAME}"
      ${EndIf}
      StrLen $0 $1
      ${If} $0 > ${MEGMEET_MAX_INSTALL_DIR}
        Pop $1
        Pop $0
        Abort
      ${EndIf}
      Pop $1
      Pop $0
    FunctionEnd
  !endif
  !ifdef BUILD_UNINSTALLER
    Function un.megmeetTargetRunning
  !else
    Function megmeetTargetRunning
  !endif
    Push $0
    Push $1
    Push $2
    Push $3
    Push $4
    Push $5
    Push $6
    Push $7
    Push $8
    StrCpy $megmeetRunningResult 0
    System::Call 'kernel32::CreateToolhelp32Snapshot(i 2, i 0) p.r0'
    StrCmp $0 -1 megmeet_snapshot_failed
    System::Alloc 556
    Pop $1
    StrCmp $1 0 megmeet_alloc_failed
    System::Call '*$1(i 556)'
    System::Call 'kernel32::Process32FirstW(p r0, p r1) i.r2'
    megmeet_process_loop:
      StrCmp $2 0 megmeet_process_done
      System::Call '*$1(i, i, i.r3, i, i, i, i, i, i, &w260.r4)'
      StrCmp $4 "${APP_EXECUTABLE_FILENAME}" 0 megmeet_next_process
      System::Call 'kernel32::OpenProcess(i 0x1000, i 0, i r3) p.r5'
      StrCmp $5 0 megmeet_query_failed
      StrCpy $6 ${NSIS_MAX_STRLEN}
      System::Call 'kernel32::QueryFullProcessImageNameW(p r5, i 0, w.r7, *i r6) i.r8'
      System::Call 'kernel32::CloseHandle(p r5)'
      StrCmp $8 0 megmeet_query_failed
      StrCmp $7 "$INSTDIR\${APP_EXECUTABLE_FILENAME}" megmeet_target_found megmeet_next_process
    megmeet_target_found:
      StrCpy $megmeetRunningResult 1
      Goto megmeet_process_done
    megmeet_query_failed:
      StrCpy $megmeetRunningResult 2
      Goto megmeet_process_done
    megmeet_next_process:
      System::Call 'kernel32::Process32NextW(p r0, p r1) i.r2'
      Goto megmeet_process_loop
    megmeet_process_done:
      System::Free $1
    megmeet_alloc_failed:
      StrCmp $1 0 0 +2
      StrCpy $megmeetRunningResult 2
      System::Call 'kernel32::CloseHandle(p r0)'
      Goto megmeet_process_end
    megmeet_snapshot_failed:
      StrCpy $megmeetRunningResult 2
    megmeet_process_end:
      Pop $8
      Pop $7
      Pop $6
      Pop $5
      Pop $4
      Pop $3
      Pop $2
      Pop $1
      Pop $0
  FunctionEnd
!macroend

; Never terminate an employee's running chat, including during a silent update.
; If this installation is still running, wait briefly then keep the old version.
!macro customCheckAppRunning
  !ifndef BUILD_UNINSTALLER
    ; Runs before uninstallOldVersion, extraction or installation registry
    ; writes. Silent /D= updates never modify an unsupported old installation.
    StrLen $R0 $INSTDIR
    ${If} $R0 > ${MEGMEET_MAX_INSTALL_DIR}
      MessageBox MB_OK|MB_ICONINFORMATION "安装目录过长（最多 ${MEGMEET_MAX_INSTALL_DIR} 个字符）。请重新运行安装器并选择较短目录。原软件和聊天资料未更改。" /SD IDOK
      SetErrorLevel 4
      Quit
    ${EndIf}
  !endif
  StrCpy $R1 0
  megmeet_wait_for_exit:
    !ifdef BUILD_UNINSTALLER
      Call un.megmeetTargetRunning
    !else
      Call megmeetTargetRunning
    !endif
    ${If} $megmeetRunningResult != 0
      IntOp $R1 $R1 + 1
      ${If} $R1 < 30
        Sleep 1000
        Goto megmeet_wait_for_exit
      ${EndIf}
      IfSilent megmeet_still_running
      MessageBox MB_OK|MB_ICONINFORMATION "请先退出麦米办公助手，再安装更新。聊天和工作文件会保留。"
      megmeet_still_running:
      SetErrorLevel 2
      Quit
    ${EndIf}
!macroend
