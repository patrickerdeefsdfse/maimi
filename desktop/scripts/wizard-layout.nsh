!ifndef BUILD_UNINSTALLER
  ; Reposition existing NSIS controls by fractions of the page, so keyboard
  ; handling, path validation, localization and DPI scaling remain native.
  !macro WizardPlace control left top width height
    IntOp $R3 $R1 * ${left}
    IntOp $R3 $R3 / 100
    IntOp $R4 $R2 * ${top}
    IntOp $R4 $R4 / 100
    IntOp $R5 $R1 * ${width}
    IntOp $R5 $R5 / 100
    IntOp $R6 $R2 * ${height}
    IntOp $R6 $R6 / 100
    System::Call 'user32::MoveWindow(p ${control},i R3,i R4,i R5,i R6,i 1)'
  !macroend
  !macro WizardMeasure page
    System::Store S
    System::Call '*(&i4 0,&i4 0,&i4 0,&i4 0)p.R0'
    System::Call 'user32::GetClientRect(p ${page},p R0)'
    System::Call '*$R0(i,i,i.R1,i.R2)'
    System::Free $R0
  !macroend
!endif

!macro customHeader
  !ifndef BUILD_UNINSTALLER
    Caption "麦米办公助手 安装"
    SetFont /LANG=2052 "Microsoft YaHei UI" 9
    Function MaimiDirectoryShow
      !insertmacro WizardMeasure $mui.DirectoryPage
      !insertmacro WizardPlace $mui.DirectoryPage.Text 0 3 100 20
      !insertmacro WizardPlace $mui.DirectoryPage.DirectoryBox 0 27 100 34
      !insertmacro WizardPlace $mui.DirectoryPage.Directory 3 39 94 15
      !insertmacro WizardPlace $mui.DirectoryPage.BrowseButton 74 68 26 17
      !insertmacro WizardPlace $mui.DirectoryPage.SpaceRequired 0 69 70 11
      !insertmacro WizardPlace $mui.DirectoryPage.SpaceAvailable 0 81 70 11
      System::Store L
    FunctionEnd
    Function MaimiProgressShow
      !insertmacro WizardMeasure $mui.InstFilesPage
      !insertmacro WizardPlace $mui.InstFilesPage.Text 0 9 100 14
      !insertmacro WizardPlace $mui.InstFilesPage.ProgressBar 0 28 100 8
      System::Store L
    FunctionEnd
  !endif
!macroend

