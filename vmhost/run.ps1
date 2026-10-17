<#
  Starts the VM host monitor on the Windows host and opens the dashboard.
  Run from an elevated PowerShell for per-vCPU load and instant VM names:
    powershell -ExecutionPolicy Bypass -File .\run.ps1
  Needs Python 3.10+ (winget install Python.Python.3.12). Everything else is
  installed into .venv in this folder; delete the folder to remove it.
#>
# 'Continue': under 'Stop', Windows PowerShell 5.1 turns any native stderr (even with 2>$null) into a terminating error.
# Native exit codes are checked explicitly instead.
$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath $PSScriptRoot

# Find a real Python 3.10+. Skip the WindowsApps "python.exe" stub, which only opens the Microsoft Store.
function Find-Python {
  $candidates = @()
  if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @('py', '-3') }
  Get-Command python, python3 -All -ErrorAction SilentlyContinue |
    Where-Object { $_.Source -notmatch '\\WindowsApps\\' } |
    ForEach-Object { $candidates += , @($_.Source) }
  foreach ($c in $candidates) {
    $exe = $c[0]; $pre = @($c | Select-Object -Skip 1)
    try {
      # no double quotes in the -c code: Windows PowerShell 5.1 strips them from native arguments
      $v = & $exe @pre -c 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])' 2>$null
      if ($LASTEXITCODE -eq 0 -and [int]$v -ge 310) { return , $c }
    } catch {}
  }
  return $null
}

$venvPy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
  $py = Find-Python
  if (-not $py) {
    Write-Host 'Python 3.10 or newer was not found.' -ForegroundColor Red
    Write-Host 'Install it, then open a NEW PowerShell window and run this script again:' -ForegroundColor Yellow
    Write-Host '  winget install --id Python.Python.3.12 --scope user' -ForegroundColor White
    exit 1
  }
  Write-Host "Creating virtual environment with $($py -join ' ')..." -ForegroundColor Cyan
  $exe = $py[0]; $pre = @($py | Select-Object -Skip 1)
  & $exe @pre -m venv (Join-Path $PSScriptRoot '.venv')
  if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv' }
}

# (Re)install psutil if a previous attempt did not finish
& $venvPy -c 'import psutil' 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host 'Installing psutil...' -ForegroundColor Cyan
  & $venvPy -m pip install --quiet --disable-pip-version-check psutil
  if ($LASTEXITCODE -ne 0) { throw 'pip install psutil failed (check internet / proxy)' }
}

$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $elevated) { Write-Host 'Not elevated: per-vCPU load will be hidden. Re-run as Administrator to see it.' -ForegroundColor Yellow }

$cfg = Get-Content (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json
$url = "http://$($cfg.listen):$($cfg.port)/"
Write-Host "Dashboard: $url   (Ctrl+C to stop)" -ForegroundColor Green
$null = Start-Job -ScriptBlock { param($u) Start-Sleep 3; Start-Process $u } -ArgumentList $url
& $venvPy (Join-Path $PSScriptRoot 'vmhost.py')
