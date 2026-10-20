<#
  Creates "VM Host Monitor" shortcuts on the desktop and in the Start menu
  (so it can also be pinned to the taskbar). No admin rights needed.

    powershell -ExecutionPolicy Bypass -File .\install-shortcut.ps1           # create
    powershell -ExecutionPolicy Bypass -File .\install-shortcut.ps1 -Remove   # remove

  Run it again if you move the folder.
#>
param([switch]$Remove)

$here = $PSScriptRoot
$shell = New-Object -ComObject WScript.Shell
$places = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))

foreach ($dir in $places) {
  $path = Join-Path $dir 'VM Host Monitor.lnk'
  if ($Remove) {
    if (Test-Path $path) { Remove-Item $path; Write-Host "Removed $path" }
    continue
  }
  $lnk = $shell.CreateShortcut($path)
  $lnk.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
  $lnk.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $here 'app.ps1')`""
  $lnk.WorkingDirectory = $here
  $lnk.IconLocation = "$(Join-Path $here 'static\vmhost.ico'),0"
  $lnk.WindowStyle = 7   # start minimised, so the launcher's console never flashes up
  $lnk.Description = 'VMware Workstation host monitor'
  $lnk.Save()
  Write-Host "Created $path" -ForegroundColor Green
}
