; Give the previous uninstaller extended paths for both sides of its Rename.
; Preserve the real install registry and the launching user's environment.
Var maimiPath
Var maimiLegacyDirectory
Var maimiSavedTmp
Var maimiSavedTemp
Var maimiLegacyFailed
Var maimiSavedDirectory
Var maimiBoundDirectory
Var maimiCurrentDirectory
Var maimiAfterUninstallError
Function MaimiExtendedPath
  Push $0
  StrCpy $0 $maimiPath 4
  StrCmp $0 "\\?\" done
  StrCpy $0 $maimiPath 2
  StrCmp $0 "\\" unc
  StrCpy $maimiPath "\\?\$maimiPath"
  Goto done
  unc:
    StrCpy $0 $maimiPath "" 2
    StrCpy $maimiPath "\\?\UNC\$0"
  done:
  Pop $0
FunctionEnd
; Old electron-builder uninstallers re-read InstallLocation during un.onInit.
; Bind that existing value to the very same directory for this call. The
; OriginalLocation marker allows the next installer to recover an interruption.
!macro MaimiRecoverLocation ROOT
  ReadRegStr $maimiSavedDirectory ${ROOT} "${INSTALL_REGISTRY_KEY}" MaimiOriginalLocation
  ${If} $maimiSavedDirectory != ""
    StrCpy $maimiPath $maimiSavedDirectory
    Call MaimiExtendedPath
    ReadRegStr $maimiCurrentDirectory ${ROOT} "${INSTALL_REGISTRY_KEY}" InstallLocation
    ${If} $maimiCurrentDirectory == $maimiPath
      WriteRegStr ${ROOT} "${INSTALL_REGISTRY_KEY}" InstallLocation "$maimiSavedDirectory"
      ${If} ${Errors}
        SetErrorLevel 2
        Abort
      ${EndIf}
    ${EndIf}
    DeleteRegValue ${ROOT} "${INSTALL_REGISTRY_KEY}" MaimiOriginalLocation
  ${EndIf}
  ClearErrors
!macroend
!macro MaimiRunOldUninstall ROOT
  ReadRegStr $maimiSavedDirectory ${ROOT} "${INSTALL_REGISTRY_KEY}" InstallLocation
  StrCpy $maimiBoundDirectory ""
  ${If} $maimiSavedDirectory != ""
    StrCpy $maimiPath $maimiSavedDirectory
    Call MaimiExtendedPath
    StrCpy $maimiBoundDirectory $maimiPath
    ClearErrors
    WriteRegStr ${ROOT} "${INSTALL_REGISTRY_KEY}" MaimiOriginalLocation "$maimiSavedDirectory"
    WriteRegStr ${ROOT} "${INSTALL_REGISTRY_KEY}" InstallLocation "$maimiBoundDirectory"
    ${If} ${Errors}
      !insertmacro MaimiRecoverLocation ${ROOT}
      SetErrorLevel 2
      Abort
    ${EndIf}
  ${EndIf}
  Push "${ROOT}"
  Call MaimiLegacyTemp
  Call uninstallOldVersion
  Call MaimiRestoreTemp
  StrCpy $maimiAfterUninstallError 0
  ${If} ${Errors}
    StrCpy $maimiAfterUninstallError 1
  ${EndIf}
  !insertmacro MaimiRecoverLocation ${ROOT}
  ${If} $maimiAfterUninstallError == 1
    SetErrors
  ${EndIf}
!macroend
Function MaimiLegacyTemp
  ReadEnvStr $maimiSavedTmp TMP
  ReadEnvStr $maimiSavedTemp TEMP
  StrCpy $maimiPath $TEMP
  Call MaimiExtendedPath
  System::Call 'kernel32::SetEnvironmentVariableW(w "TMP", w "$maimiPath") i'
  System::Call 'kernel32::SetEnvironmentVariableW(w "TEMP", w "$maimiPath") i'
  ClearErrors
FunctionEnd
Function MaimiRestoreTemp
  IfErrors 0 +3
    StrCpy $maimiLegacyFailed 1
    Goto +2
  StrCpy $maimiLegacyFailed 0
  StrCmp $maimiSavedTmp "" +3
    System::Call 'kernel32::SetEnvironmentVariableW(w "TMP", w "$maimiSavedTmp") i'
    Goto +2
  System::Call 'kernel32::SetEnvironmentVariableW(w "TMP", p 0) i'
  StrCmp $maimiSavedTemp "" +3
    System::Call 'kernel32::SetEnvironmentVariableW(w "TEMP", w "$maimiSavedTemp") i'
    Goto +2
  System::Call 'kernel32::SetEnvironmentVariableW(w "TEMP", p 0) i'
  ClearErrors
  StrCmp $maimiLegacyFailed 0 +2
    SetErrors
FunctionEnd
