  ; Keep the installed application intact until the complete embedded archive
  ; has been read, passes its CRC checks, and has the expected product files.
  CreateDirectory "$PLUGINSDIR\7z-out"
  File /oname=$PLUGINSDIR\maimi-7za.exe "@SEVEN_EXE@"
  nsExec::Exec '"$PLUGINSDIR\maimi-7za.exe" x "${FILE}" "-o$PLUGINSDIR\7z-out" -y -bd -bso0 -bsp0'
  Pop $R2
  StrCmp $R2 "0" 0 MaimiStageInvalid
  IfFileExists "$PLUGINSDIR\7z-out\${APP_EXECUTABLE_FILENAME}" 0 MaimiStageInvalid
  IfFileExists "$PLUGINSDIR\7z-out\resources\app\dsh\desktop-runtime.json" 0 MaimiStageInvalid
  IfFileExists "$PLUGINSDIR\7z-out\resources\app\package.json" MaimiStageComplete
  MaimiStageInvalid:
    SetErrorLevel 2
    IfSilent +2
      MessageBox MB_OK|MB_ICONSTOP "安装文件未能完整展开。请检查临时磁盘空间，或重新下载后再试。"
    Quit
  MaimiStageComplete:
