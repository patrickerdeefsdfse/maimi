  ; On the same volume, moving this one directory avoids copying thousands of
  ; small files twice. Across drives use the bundled long-path-aware extractor.
  StrCpy $R1 0
  ClearErrors
  Rename "$PLUGINSDIR\7z-out\resources" "$OUTDIR\resources"
  IfErrors MaimiInstallWith7zip
  ClearErrors
    IntOp $R1 $R1 + 1
    ClearErrors
    CopyFiles /SILENT "$PLUGINSDIR\7z-out\*" "$OUTDIR"
    IfErrors 0 MaimiInstalledFiles
    Goto MaimiWriteRetry
  MaimiInstallWith7zip:
    IntOp $R1 $R1 + 1
    nsExec::Exec '"$PLUGINSDIR\maimi-7za.exe" x "$PLUGINSDIR\app-$packageArch.7z" "-o$OUTDIR" -y -bd -bso0 -bsp0'
    Pop $R2
    StrCmp $R2 "0" MaimiInstalledFiles
  MaimiWriteRetry:
    ${If} $R1 < 3
      Sleep 1000
      Goto MaimiInstallWith7zip
    ${EndIf}
    IfSilent MaimiExtractFailed
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "无法写入麦米安装目录。请关闭仍在使用麦米文件的程序，检查磁盘空间后重试。" IDRETRY MaimiInstallWith7zip
  MaimiExtractFailed:
    SetErrorLevel 2
    IfSilent +2
      MessageBox MB_OK|MB_ICONSTOP "安装未完成，请检查磁盘空间和安装目录权限，再重新运行安装程序。"
    Quit
  MaimiInstalledFiles:
    SetOutPath "$INSTDIR\resources\licenses\7zip"
    File /oname=License.txt "@SEVEN_LICENSE@"
    File /oname=7z2606-src.7z "@SEVEN_SOURCE@"
    SetOutPath "$INSTDIR"
