<#
.SYNOPSIS
    chkp-monitor bootstrap script
    Run on a fresh Skillable lab A-GUI to install dependencies,
    configure API access, create monitoring accounts, and launch the health dashboard.

.USAGE
    irm https://raw.githubusercontent.com/Don-Paterson/chkp-monitor/main/bootstrap.ps1 | iex
#>

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Speed up downloads

# ---- Configuration ----
$REPO_URL      = "https://raw.githubusercontent.com/Don-Paterson/chkp-monitor/main"
$INSTALL_DIR   = "C:\chkp-monitor"
$PYTHON_URL    = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
$PS7_URL       = "https://github.com/PowerShell/PowerShell/releases/download/v7.4.6/PowerShell-7.4.6-win-x64.msi"
$PLINK         = "C:\Program Files\PuTTY\plink.exe"

# Lab defaults
$MGMT_HOST     = "10.1.1.101"
$GW01_HOST     = "10.1.1.2"
$GW02_HOST     = "10.1.1.3"
$MGMT_ADMIN    = "cpadmin"
$GAIA_ADMIN    = "admin"
$LAB_PASSWORD  = 'Chkp!234'
$MONITOR_USER  = "monitor-api"
$MONITOR_PASS  = 'M0n!t0r@pi'
$GAIA_MON_USER = "gaia_monitor_api"

Write-Host ""
Write-Host "====================================" -ForegroundColor Cyan
Write-Host "  chkp-monitor bootstrap" -ForegroundColor Cyan
Write-Host "====================================" -ForegroundColor Cyan
Write-Host ""

# ---- Helper: run command on remote host via plink ----
function Invoke-Plink {
    param(
        [string]$Host_,
        [string]$User,
        [string]$Password,
        [string]$Command
    )
    $output = & $PLINK -ssh -l $User -pw $Password -batch $Host_ $Command 2>&1
    return ($output | Out-String).Trim()
}

# ---- Helper: run multiple commands via plink stdin (for interactive sequences) ----
function Invoke-PlinkScript {
    param(
        [string]$Host_,
        [string]$User,
        [string]$Password,
        [string[]]$Commands
    )
    $script = ($Commands -join "`n") + "`nexit`n"
    $output = $script | & $PLINK -ssh -l $User -pw $Password -batch $Host_ 2>&1
    return ($output | Out-String).Trim()
}

# ---- Step 0: Accept SSH host keys ----
Write-Host "[0/8] Accepting SSH host keys..." -ForegroundColor Yellow
foreach ($h in @($MGMT_HOST, $GW01_HOST, $GW02_HOST)) {
    # Pipe 'y' to auto-accept the host key on first connection
    $result = "y" | & $PLINK -ssh -l $GAIA_ADMIN -pw $LAB_PASSWORD $h "exit" 2>&1
    Write-Host "  Key cached for $h" -ForegroundColor Green
}
Start-Sleep -Seconds 1

# ---- Step 1: Install Python ----
Write-Host "[1/8] Checking Python..." -ForegroundColor Yellow
$pythonInstalled = $false
try {
    $pyVer = & python --version 2>&1
    if ($pyVer -match "Python 3\.\d+") {
        Write-Host "  Python already installed: $pyVer" -ForegroundColor Green
        $pythonInstalled = $true
    }
} catch {}

if (-not $pythonInstalled) {
    Write-Host "  Downloading Python 3.12..." -ForegroundColor White
    $pyInstaller = "$env:TEMP\python-installer.exe"
    Invoke-WebRequest -Uri $PYTHON_URL -OutFile $pyInstaller -UseBasicParsing

    Write-Host "  Installing Python (silent)..." -ForegroundColor White
    Start-Process -FilePath $pyInstaller -ArgumentList `
        "/quiet", "InstallAllUsers=1", "PrependPath=1", `
        "Include_pip=1", "Include_test=0" `
        -Wait -NoNewWindow

    # Refresh PATH for this session
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")

    Write-Host "  Python installed successfully" -ForegroundColor Green
}

# ---- Step 2: Install PowerShell 7 (optional) ----
Write-Host "[2/8] Checking PowerShell 7..." -ForegroundColor Yellow
$ps7Path = "C:\Program Files\PowerShell\7\pwsh.exe"
if (Test-Path $ps7Path) {
    Write-Host "  PowerShell 7 already installed" -ForegroundColor Green
} else {
    Write-Host "  Downloading PowerShell 7.4..." -ForegroundColor White
    $ps7Installer = "$env:TEMP\ps7-installer.msi"
    Invoke-WebRequest -Uri $PS7_URL -OutFile $ps7Installer -UseBasicParsing

    Write-Host "  Installing PowerShell 7 (silent)..." -ForegroundColor White
    Start-Process -FilePath "msiexec.exe" -ArgumentList `
        "/i", "`"$ps7Installer`"", "/quiet", "/norestart", `
        "ADD_EXPLORER_CONTEXT_MENU_OPENPOWERSHELL=1", `
        "ADD_FILE_CONTEXT_MENU_RUNPOWERSHELL=1", `
        "ENABLE_PSREMOTING=0", "REGISTER_MANIFEST=1" `
        -Wait -NoNewWindow

    Write-Host "  PowerShell 7 installed" -ForegroundColor Green
}

# ---- Step 3: Install Python packages ----
Write-Host "[3/8] Installing Python packages..." -ForegroundColor Yellow
& python -m pip install --quiet --upgrade pip 2>$null
& python -m pip install --quiet flask requests paramiko 2>$null
Write-Host "  Flask, requests, paramiko installed" -ForegroundColor Green

# ---- Step 4: Download chkp-monitor files ----
Write-Host "[4/8] Downloading chkp-monitor..." -ForegroundColor Yellow

if (Test-Path $INSTALL_DIR) {
    Remove-Item -Recurse -Force $INSTALL_DIR
}
New-Item -ItemType Directory -Path $INSTALL_DIR -Force | Out-Null
New-Item -ItemType Directory -Path "$INSTALL_DIR\collectors" -Force | Out-Null
New-Item -ItemType Directory -Path "$INSTALL_DIR\static" -Force | Out-Null

$files = @(
    "config.json",
    "server.py",
    "collectors/__init__.py",
    "collectors/mgmt_api.py",
    "collectors/gaia_api.py",
    "collectors/ssh_fallback.py",
    "static/dashboard.html"
)

foreach ($file in $files) {
    $url = "$REPO_URL/$file"
    $dest = "$INSTALL_DIR\$($file -replace '/', '\')"
    Write-Host "  Downloading $file..." -ForegroundColor White
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}

Write-Host "  Files downloaded to $INSTALL_DIR" -ForegroundColor Green

# ---- Step 5: Configure Management API on A-SMS ----
Write-Host "[5/8] Configuring Management API on A-SMS ($MGMT_HOST)..." -ForegroundColor Yellow

try {
    # 5a: Create monitor-api administrator (read-only)
    Write-Host "  Creating $MONITOR_USER administrator..." -ForegroundColor White
    $cmd = "mgmt_cli -r true add administrator name `"$MONITOR_USER`" password `"$MONITOR_PASS`" must-change-password false permissions-profile `"Read Only All`" --format json"
    $result = Invoke-Plink -Host_ $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $cmd

    if ($result -match "already exists") {
        Write-Host "  $MONITOR_USER already exists on A-SMS" -ForegroundColor Green
    } elseif ($result -match '"uid"') {
        Write-Host "  $MONITOR_USER created on A-SMS" -ForegroundColor Green
    } else {
        Write-Host "  add administrator output: $($result.Substring(0, [Math]::Min(300, $result.Length)))" -ForegroundColor Yellow
    }

    # 5b: Open management API to all IP addresses
    Write-Host "  Setting API access to all IP addresses..." -ForegroundColor White
    $cmd2 = "mgmt_cli -r true set api-settings accepted-api-calls-from `"All IP addresses`" --format json"
    $result2 = Invoke-Plink -Host_ $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $cmd2
    Write-Host "  API access configured" -ForegroundColor Green

    # 5c: Publish
    Write-Host "  Publishing changes..." -ForegroundColor White
    $cmd3 = "mgmt_cli -r true publish --format json"
    $result3 = Invoke-Plink -Host_ $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $cmd3
    Write-Host "  Published" -ForegroundColor Green

    # 5d: Restart API to apply accepted-api-calls-from change
    Write-Host "  Restarting management API (may take up to 2 minutes)..." -ForegroundColor White
    $cmd4 = "api restart"
    $result4 = Invoke-Plink -Host_ $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $cmd4
    Write-Host "  Management API restart initiated" -ForegroundColor Green

} catch {
    Write-Host "  Warning: Management API setup issue: $_" -ForegroundColor Red
    Write-Host "  Manual fix: SSH to A-SMS and run the mgmt_cli commands from the README" -ForegroundColor Yellow
}

# ---- Step 6: Wait for Management API to come back ----
Write-Host "[6/8] Waiting for Management API to be ready..." -ForegroundColor Yellow

# Skip certificate check for self-signed certs (PS7 compatible)
$PSDefaultParameterValues['Invoke-RestMethod:SkipCertificateCheck'] = $true

$mgmtReady = $false
for ($i = 1; $i -le 30; $i++) {
    try {
        $testBody = @{ user = $MONITOR_USER; password = $MONITOR_PASS } | ConvertTo-Json
        $testResp = Invoke-RestMethod -Uri "https://${MGMT_HOST}/web_api/login" -Method Post `
            -Body $testBody -ContentType "application/json" -TimeoutSec 5
        if ($testResp.sid) {
            # Logout cleanly
            $logoutHeaders = @{ "X-chkp-sid" = $testResp.sid }
            Invoke-RestMethod -Uri "https://${MGMT_HOST}/web_api/logout" -Method Post `
                -Body "{}" -ContentType "application/json" -Headers $logoutHeaders -TimeoutSec 5 | Out-Null
            $mgmtReady = $true
            break
        }
    } catch {}
    Write-Host "  Waiting... ($i/30)" -ForegroundColor White
    Start-Sleep -Seconds 10
}

if ($mgmtReady) {
    Write-Host "  Management API is ready and accepting remote connections" -ForegroundColor Green
} else {
    Write-Host "  Management API not responding yet - dashboard will keep retrying" -ForegroundColor Yellow
}

# ---- Step 7: Create Gaia API users on gateways ----
Write-Host "[7/8] Configuring Gaia API users on gateways..." -ForegroundColor Yellow

foreach ($gwHost in @($GW01_HOST, $GW02_HOST)) {
    $gwName = if ($gwHost -eq $GW01_HOST) { "A-GW-01" } else { "A-GW-02" }

    try {
        # 7a: Create user and set password via Clish
        Write-Host "  [$gwName] Creating user $GAIA_MON_USER..." -ForegroundColor White
        $clishCommands = @(
            "add user $GAIA_MON_USER uid 0 homedir /home/$GAIA_MON_USER"
            "set user $GAIA_MON_USER password"
            "$LAB_PASSWORD"
            "$LAB_PASSWORD"
            "add rba user $GAIA_MON_USER roles adminRole"
        )
        $result = Invoke-PlinkScript -Host_ $gwHost -User $GAIA_ADMIN -Password $LAB_PASSWORD -Commands $clishCommands

        if ($result -match "already exists") {
            Write-Host "  [$gwName] User already exists" -ForegroundColor Green
        } else {
            Write-Host "  [$gwName] User created" -ForegroundColor Green
        }

        # 7b: Enable Gaia API access (requires expert mode)
        Write-Host "  [$gwName] Enabling Gaia API access..." -ForegroundColor White
        $expertCommands = @(
            "expert"
            "$LAB_PASSWORD"
            "gaia_api access --user $GAIA_MON_USER --enable true"
            "exit"
        )
        $result2 = Invoke-PlinkScript -Host_ $gwHost -User $GAIA_ADMIN -Password $LAB_PASSWORD -Commands $expertCommands
        Write-Host "  [$gwName] Gaia API access enabled" -ForegroundColor Green

    } catch {
        Write-Host "  Warning: Could not configure $GAIA_MON_USER on ${gwName}: $_" -ForegroundColor Red
        Write-Host "  Manual fix: SSH to $gwHost and create user with Gaia API access" -ForegroundColor Yellow
    }
}

# ---- Step 8: Create credentials.json and launch ----
Write-Host "[8/8] Creating credentials and launching..." -ForegroundColor Yellow

$credentials = @{
    management = @{
        user = $MONITOR_USER
        password = $MONITOR_PASS
    }
    gaia = @{
        user = $GAIA_MON_USER
        password = $LAB_PASSWORD
    }
    bootstrap = @{
        mgmt_admin_user = $MGMT_ADMIN
        mgmt_admin_password = $LAB_PASSWORD
        gaia_admin_user = $GAIA_ADMIN
        gaia_admin_password = $LAB_PASSWORD
    }
}

$credentials | ConvertTo-Json -Depth 3 | Set-Content "$INSTALL_DIR\credentials.json" -Encoding UTF8

Write-Host ""
Write-Host "====================================" -ForegroundColor Green
Write-Host "  chkp-monitor installed!" -ForegroundColor Green
Write-Host "====================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Location:  $INSTALL_DIR" -ForegroundColor White
Write-Host "  Dashboard: http://localhost:8080" -ForegroundColor White
Write-Host ""
Write-Host "  Mgmt API user:  $MONITOR_USER / $MONITOR_PASS" -ForegroundColor White
Write-Host "  Gaia API user:  $GAIA_MON_USER / $LAB_PASSWORD" -ForegroundColor White
Write-Host ""
Write-Host "Starting dashboard..." -ForegroundColor Yellow
Write-Host "(Press Ctrl+C to stop)" -ForegroundColor Gray
Write-Host ""

# Launch the server
Set-Location $INSTALL_DIR
& python server.py
