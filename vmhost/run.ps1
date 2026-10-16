<#
  Starts the VM host monitor on the Windows host and opens the dashboard.
  Run from an elevated PowerShell for per-vCPU load and instant VM names:
    powershell -ExecutionPolicy Bypass -File .\run.ps1
#>
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$py = Get-Command py, python -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $py) { throw 'Python 3.10+ not found. Install it with: winget install Python.Python.3.12' }

$venv = Join-Path $PSScriptRoot '.venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
  Write-Host 'Creating virtual environment...' -ForegroundColor Cyan
  & $py.Source -m venv $venv
  & (Join-Path $venv 'Scripts\python.exe') -m pip install --quiet --upgrade pip psutil
}

$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $elevated) { Write-Host 'Not elevated: per-vCPU load will be hidden. Re-run as Administrator to see it.' -ForegroundColor Yellow }

$cfg = Get-Content (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json
$url = "http://$($cfg.listen):$($cfg.port)/"
$null = Start-Job { Start-Sleep 3; Start-Process $using:url }
& (Join-Path $venv 'Scripts\python.exe') (Join-Path $PSScriptRoot 'vmhost.py')
