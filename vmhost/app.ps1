<#
  VM Host Monitor - desktop app launcher (what the desktop / Start menu shortcut runs).

  1. First time only: runs run.ps1 -SetupOnly in a visible window (creates .venv, installs psutil).
  2. Starts the monitor hidden and elevated (one UAC prompt; elevation is needed for per-vCPU load).
     If UAC is declined it still starts, without per-vCPU load.
  3. Opens the dashboard in its own app window (Edge or Chrome --app: no tabs, no address bar).
  4. When that window is closed, the monitor stops.

  If the monitor is already running, it just opens another window.
  Log of the hidden monitor: vmhost.log in this folder.
#>
$ErrorActionPreference = 'Continue'
$here = $PSScriptRoot
Add-Type -AssemblyName PresentationFramework

function Show-Message([string]$text, [string]$icon = 'Error') {
  [System.Windows.MessageBox]::Show($text, 'VM Host Monitor', 'OK', $icon) | Out-Null
}

$cfg = Get-Content (Join-Path $here 'config.json') -Raw | ConvertFrom-Json
$url = "http://$($cfg.listen):$($cfg.port)/"

function Test-MonitorUp {
  try {
    $null = Invoke-WebRequest "$($url)api/series" -UseBasicParsing -TimeoutSec 2
    return $true
  } catch { return $false }
}

# ---- 1. First-time setup ---------------------------------------------------
$venvPy = Join-Path $here '.venv\Scripts\python.exe'
$venvPyw = Join-Path $here '.venv\Scripts\pythonw.exe'
$ready = $false
if (Test-Path $venvPyw) { & $venvPy -c 'import psutil' 2>$null; $ready = ($LASTEXITCODE -eq 0) }
if (-not $ready) {
  $setup = Start-Process powershell.exe -PassThru -Wait -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$(Join-Path $here 'run.ps1')`"", '-SetupOnly')
  if ($setup.ExitCode -ne 0 -or -not (Test-Path $venvPyw)) {
    Show-Message "First-time setup did not finish.`n`nInstall Python with:`n  winget install --id Python.Python.3.12 --scope user`nthen start VM Host Monitor again."
    exit 1
  }
}

# ---- 2. Start the monitor (hidden, elevated) -------------------------------
if (-not (Test-MonitorUp)) {
  $log = Join-Path $here 'vmhost.log'
  $monitorArgs = @("`"$(Join-Path $here 'vmhost.py')`"", '--exit-with', $PID, '--log', "`"$log`"")
  try {
    Start-Process $venvPyw -ArgumentList $monitorArgs -WorkingDirectory $here -Verb RunAs -WindowStyle Hidden -ErrorAction Stop
  } catch {
    # UAC declined: run without elevation (the dashboard notes that per-vCPU load is hidden)
    Start-Process $venvPyw -ArgumentList $monitorArgs -WorkingDirectory $here -WindowStyle Hidden
  }
  $deadline = (Get-Date).AddSeconds(30)
  while (-not (Test-MonitorUp)) {
    if ((Get-Date) -gt $deadline) {
      Show-Message "The monitor did not start within 30 seconds.`n`nSee the log:`n$log"
      exit 1
    }
    Start-Sleep -Milliseconds 500
  }
}

# ---- 3. Open the app window and wait for it to close ------------------------
$browser = @(
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if ($browser) {
  # A profile of its own makes this a separate browser process we can wait on,
  # and remembers the window size and position.
  $appProfile = Join-Path $env:LOCALAPPDATA 'VMHostMonitor\browser'
  $window = Start-Process $browser -PassThru -ArgumentList @(
    "--app=$url", "--user-data-dir=`"$appProfile`"", '--no-first-run', '--no-default-browser-check',
    '--window-size=1600,1000')
  $window.WaitForExit()
} else {
  Start-Process $url
  Show-Message "VM Host Monitor is running in your browser.`n`nClick OK to stop it." 'Information'
}
# ---- 4. Leaving here ends this process; the monitor (--exit-with) stops itself ----
