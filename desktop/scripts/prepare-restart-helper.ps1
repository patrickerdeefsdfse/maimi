<# Compile the x86 DWM helper and raster assets embedded in the NSIS installer. #>
[CmdletBinding()]
param([string]$OutputDirectory, [switch]$TestProgress, [switch]$CompileProgressOnly)
$ErrorActionPreference = 'Stop'
$installerRoot = Join-Path $PSScriptRoot '../installer'
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $env:MAIMI_WORK 'installer-ui' }
$output = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force $output | Out-Null
$source = Join-Path $installerRoot 'window-frame.cpp'
$library = Join-Path $output 'window-frame.dll'
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
if (-not (Test-Path -LiteralPath $vswhere)) { throw 'Windows installer preparation requires Visual Studio C++ Build Tools and a Windows SDK.' }
$visualStudio = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $visualStudio) { throw 'Visual Studio C++ Build Tools are missing.' }
$vcvars = Join-Path $visualStudio 'VC/Auxiliary/Build/vcvars32.bat'
$compileScript = Join-Path $output 'compile-frame.cmd'
$compileLines = @('@echo off', ('call "{0}" >nul' -f $vcvars), 'if errorlevel 1 exit /b %errorlevel%', ('cl /nologo /LD /MT /O1 /W4 /WX /EHsc "{0}" /Fo"{1}" /link /OUT:"{2}" /IMPLIB:"{3}" user32.lib gdi32.lib comctl32.lib dwmapi.lib gdiplus.lib ole32.lib shell32.lib uuid.lib advapi32.lib' -f $source, (Join-Path $output 'window-frame.obj'), $library, (Join-Path $output 'window-frame.lib')))
[IO.File]::WriteAllLines($compileScript, $compileLines, [Text.Encoding]::Default)
& $env:ComSpec /d /c $compileScript
if ($LASTEXITCODE -ne 0) { throw 'Native installer helper compilation failed.' }
