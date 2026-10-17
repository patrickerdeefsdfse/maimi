param(
    [ValidateSet('status','keepalive','up','start','restart','restart-dsh','stop','smoke','native-smoke','test','lock','shell','retrieval-start','retrieval-stop','retrieval-status','retrieval-verify')]
    [string]$Action = 'status'
)
$ErrorActionPreference = 'Stop'
# WSL emits UTF-8 paths. Windows PowerShell 5.1 otherwise misdecodes Chinese
# workspace names when launched by the desktop application.
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$anchorFile = 'D:\MegmeetDev\wsl-anchor.json'
function Get-DevAnchor {
    if (-not (Test-Path -LiteralPath $anchorFile)) { return $null }
    $saved = Get-Content -LiteralPath $anchorFile -Raw | ConvertFrom-Json
    $proc = Get-Process -Id $saved.pid -ErrorAction SilentlyContinue
    if ($null -eq $proc -or $proc.ProcessName -ne 'wsl') { return $null }
    if ($proc.StartTime.ToUniversalTime().Ticks -ne $saved.startTicks) { return $null }
    return $proc
}
# systemd services alone do not keep a WSL distribution alive after its last
# interactive process exits. Hold a hidden, idle client for the development stack.
if ($Action -in @('up','start','keepalive','retrieval-start') -and $null -eq (Get-DevAnchor)) {
    $anchor = Start-Process -FilePath "$env:WINDIR\System32\wsl.exe" `
        -ArgumentList @('--distribution','MegmeetDev','--user','megmeet','--exec','sleep','infinity') `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput 'D:\MegmeetDev\wsl-anchor.log' `
        -RedirectStandardError 'D:\MegmeetDev\wsl-anchor-error.log'
    @{pid=$anchor.Id; startTicks=$anchor.StartTime.ToUniversalTime().Ticks} |
        ConvertTo-Json | Set-Content -LiteralPath $anchorFile -Encoding utf8
}
if ($Action -eq 'keepalive') { Write-Output 'LOCAL_DEVELOPMENT_KEEPALIVE: READY'; exit 0 }
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$entry = Join-Path $PSScriptRoot 'run-dev.sh'
$linuxEntry = & wsl.exe --distribution MegmeetDev --user megmeet --exec wslpath -a $entry
if ($LASTEXITCODE -ne 0) { throw 'MegmeetDev WSL environment is not ready.' }
if ($Action -eq 'shell') {
    & wsl.exe --distribution MegmeetDev --user megmeet --cd $projectRoot
} else {
    & wsl.exe --distribution MegmeetDev --user megmeet --exec bash $linuxEntry.Trim() $Action
}
$result = $LASTEXITCODE
if ($Action -eq 'stop' -and $result -eq 0) {
    $anchor = Get-DevAnchor
    if ($null -ne $anchor) { Stop-Process -Id $anchor.Id }
}
exit $result
