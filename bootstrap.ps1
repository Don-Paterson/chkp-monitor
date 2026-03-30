<#
.SYNOPSIS
    chkp-monitor bootstrap script
    Run on a fresh Skillable lab A-GUI to install dependencies,
    configure API access, create monitoring accounts, and launch the health dashboard.

.USAGE
    irm https://raw.githubusercontent.com/Don-Paterson/chkp-monitor/main/bootstrap.ps1 | iex

.NOTES
    - mgmt_cli requires expert mode (bash shell), achieved by setting admin shell to /bin/bash
    - mgmt_cli admin commands require domain "System Data" and a session
    - Gaia API access must be enabled per-user via gaia_api command in expert mode
    - plink.exe is pre-installed at C:\Program Files\PuTTY\
    - save config persists Gaia changes across reboots
#>

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

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
$GAIA_ADMIN    = "admin"
$MGMT_ADMIN    = "cpadmin"
$LAB_PASSWORD  = 'Chkp!234'
$MONITOR_USER  = "monitor-api"
$MONITOR_PASS  = 'M0n!t0r@pi'
$GAIA_MON_USER = "gaia_monitor_api"
$GAIA_MON_PASS = 'M0n1t3r321'

# Skip SSL cert validation for self-signed Check Point certs (PS7 compatible)
$PSDefaultParameterValues['Invoke-RestMethod:SkipCertificateCheck'] = $true
$PSDefaultParameterValues['Invoke-WebRequest:SkipCertificateCheck'] = $true

Write-Host ""
Write-Host "====================================" -ForegroundColor Cyan
Write-Host "  chkp-monitor bootstrap" -ForegroundColor Cyan
Write-Host "====================================" -ForegroundColor Cyan
Write-Host ""

# ---- Helper: run a single command via plink ----
function Invoke-Plink {
    param(
        [string]$RemoteHost,
        [string]$User,
        [string]$Password,
        [string]$Command
    )
    $output = & $PLINK -ssh -l $User -pw $Password -batch $RemoteHost $Command 2>&1
    return ($output | Out-String).Trim()
}

# ---- Helper: switch a Gaia user's shell to bash (expert mode) ----
function Set-ExpertShell {
    param([string]$RemoteHost, [string]$User, [string]$Password)
    $null = Invoke-Plink -RemoteHost $RemoteHost -User $User -Password $Password -Command "lock database override"
    $null = Invoke-Plink -RemoteHost $RemoteHost -User $User -Password $Password -Command "set user $User shell /bin/bash"
}

# ---- Helper: restore a Gaia user's shell to Clish ----
function Set-ClishShell {
    param([string]$RemoteHost, [string]$User, [string]$Password)
    # Run clish -c from bash to set shell back
    $null = Invoke-Plink -RemoteHost $RemoteHost -User $User -Password $Password -Command "clish -c 'set user $User shell /etc/cli.sh'"
}

# ---- Step 0: Accept SSH host keys ----
Write-Host "[0/8] Accepting SSH host keys..." -ForegroundColor Yellow
foreach ($h in @($MGMT_HOST, $GW01_HOST, $GW02_HOST)) {
    $null = "y" | & $PLINK -ssh -l $GAIA_ADMIN -pw $LAB_PASSWORD $h "exit" 2>&1
    Write-Host "  Key cached for $h" -ForegroundColor Green
}
Start-Sleep -Seconds 2

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

    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")

    Write-Host "  Python installed successfully" -ForegroundColor Green
}

# ---- Step 2: Install PowerShell 7 ----
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

# ============================================================
# Step 5: Configure Management API on A-SMS
# ============================================================
# Strategy: switch admin shell to /bin/bash so plink commands
# run in expert mode, then use mgmt_cli with session + domain
# "System Data" for all admin operations.
# ============================================================
Write-Host "[5/8] Configuring Management API on A-SMS ($MGMT_HOST)..." -ForegroundColor Yellow

try {
    # 5a: Switch admin to expert mode (bash shell)
    Write-Host "  Switching admin shell to expert mode..." -ForegroundColor White
    Set-ExpertShell -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD
    Write-Host "  Admin shell set to /bin/bash" -ForegroundColor Green

    # 5b: Login to mgmt_cli with domain "System Data"
    Write-Host "  Logging in (domain: System Data)..." -ForegroundColor White
    $loginCmd = "mgmt_cli login user $MGMT_ADMIN password '$LAB_PASSWORD' domain 'System Data' --format json > /tmp/chkp-mon-sid.txt 2>&1 && echo LOGIN_OK || echo LOGIN_FAIL"
    $loginResult = Invoke-Plink -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $loginCmd

    if ($loginResult -match "LOGIN_OK") {
        Write-Host "  Logged in" -ForegroundColor Green

        # 5c: Create monitor-api administrator (read-only)
        Write-Host "  Creating $MONITOR_USER administrator..." -ForegroundColor White
        $addCmd = "mgmt_cli add administrator name '$MONITOR_USER' password '$MONITOR_PASS' must-change-password false permissions-profile 'Read Only All' -s /tmp/chkp-mon-sid.txt --format json 2>&1"
        $addResult = Invoke-Plink -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $addCmd

        if ($addResult -match "already exists") {
            Write-Host "  $MONITOR_USER already exists" -ForegroundColor Green
        } elseif ($addResult -match "uid") {
            Write-Host "  $MONITOR_USER created" -ForegroundColor Green
        } else {
            Write-Host "  Result: $($addResult.Substring(0, [Math]::Min(200, $addResult.Length)))" -ForegroundColor Yellow
        }

        # 5d: Open management API to all IP addresses
        Write-Host "  Setting API access to all IP addresses..." -ForegroundColor White
        $apiCmd = "mgmt_cli set api-settings accepted-api-calls-from 'All IP addresses' -s /tmp/chkp-mon-sid.txt --format json 2>&1"
        $null = Invoke-Plink -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $apiCmd
        Write-Host "  API access configured" -ForegroundColor Green

        # 5e: Publish
        Write-Host "  Publishing..." -ForegroundColor White
        $pubCmd = "mgmt_cli publish -s /tmp/chkp-mon-sid.txt --format json 2>&1"
        $pubResult = Invoke-Plink -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $pubCmd
        if ($pubResult -match "succeeded") {
            Write-Host "  Published" -ForegroundColor Green
        } else {
            Write-Host "  Publish output: $($pubResult.Substring(0, [Math]::Min(200, $pubResult.Length)))" -ForegroundColor Yellow
        }

        # 5f: Logout and clean up
        $null = Invoke-Plink -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command "mgmt_cli logout -s /tmp/chkp-mon-sid.txt 2>&1; rm -f /tmp/chkp-mon-sid.txt"

        # 5g: Restart API to apply accepted-api-calls-from
        Write-Host "  Restarting management API..." -ForegroundColor White
        $restartResult = Invoke-Plink -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command "api restart 2>&1"
        if ($restartResult -match "started successfully") {
            Write-Host "  API restarted successfully" -ForegroundColor Green
        } else {
            Write-Host "  Restart output: $($restartResult.Substring(0, [Math]::Min(200, $restartResult.Length)))" -ForegroundColor Yellow
        }

    } else {
        Write-Host "  Login failed: $($loginResult.Substring(0, [Math]::Min(200, $loginResult.Length)))" -ForegroundColor Red
    }

    # 5h: Restore admin shell to Clish
    Write-Host "  Restoring admin shell to Clish..." -ForegroundColor White
    Set-ClishShell -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD
    Write-Host "  Shell restored" -ForegroundColor Green

} catch {
    Write-Host "  Warning: Management API setup error: $_" -ForegroundColor Red
    Write-Host "  Manual fix: SSH to A-SMS (see README)" -ForegroundColor Yellow
    # Try to restore shell even on error
    try { Set-ClishShell -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD } catch {}
}

# ---- Step 6: Verify Management API accepts remote connections ----
Write-Host "[6/8] Verifying Management API accepts remote connections..." -ForegroundColor Yellow

$mgmtReady = $false
for ($i = 1; $i -le 12; $i++) {
    try {
        $testBody = @{ user = $MONITOR_USER; password = $MONITOR_PASS } | ConvertTo-Json
        $testResp = Invoke-RestMethod -Uri "https://${MGMT_HOST}/web_api/login" -Method Post `
            -Body $testBody -ContentType "application/json" -TimeoutSec 5
        if ($testResp.sid) {
            $null = Invoke-RestMethod -Uri "https://${MGMT_HOST}/web_api/logout" -Method Post `
                -Body "{}" -ContentType "application/json" `
                -Headers @{ "X-chkp-sid" = $testResp.sid } -TimeoutSec 5
            $mgmtReady = $true
            break
        }
    } catch {}
    Write-Host "  Waiting for API... ($i/12)" -ForegroundColor White
    Start-Sleep -Seconds 5
}

if ($mgmtReady) {
    Write-Host "  Management API ready and accepting remote connections" -ForegroundColor Green
} else {
    Write-Host "  Management API not responding - dashboard will keep retrying" -ForegroundColor Yellow
    Write-Host "  Verify on A-SMS: api status (check Accessibility field)" -ForegroundColor Yellow
}

# ============================================================
# Step 7: Create Gaia API users on gateways
# ============================================================
# Strategy: switch admin shell to bash, create user (ignore if
# exists), ALWAYS set password via echo pipe to clish, assign
# RBA role, enable Gaia API access, save config, restore shell.
# ============================================================
Write-Host "[7/8] Configuring Gaia API users on gateways..." -ForegroundColor Yellow

foreach ($gwHost in @($GW01_HOST, $GW02_HOST)) {
    $gwName = if ($gwHost -eq $GW01_HOST) { "A-GW-01" } else { "A-GW-02" }

    try {
        # 7a: Switch to bash shell
        Write-Host "  [$gwName] Switching to expert mode..." -ForegroundColor White
        Set-ExpertShell -RemoteHost $gwHost -User $GAIA_ADMIN -Password $LAB_PASSWORD

        # 7b: Create user (ignore error if already exists)
        Write-Host "  [$gwName] Creating user $GAIA_MON_USER..." -ForegroundColor White
        $addCmd = "clish -c 'add user $GAIA_MON_USER uid 0 homedir /home/$GAIA_MON_USER' 2>&1 || true; echo ADD_DONE"
        $addResult = Invoke-Plink -RemoteHost $gwHost -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $addCmd

        if ($addResult -match "already exists") {
            Write-Host "  [$gwName] User already exists - will reset password" -ForegroundColor Green
        } else {
            Write-Host "  [$gwName] User created" -ForegroundColor Green
        }

        # 7c: Set password (always, whether user is new or existing)
        Write-Host "  [$gwName] Setting password..." -ForegroundColor White
        $pwCmd = "echo -e '$GAIA_MON_PASS\n$GAIA_MON_PASS' | clish -c 'set user $GAIA_MON_USER password' 2>&1; echo PW_DONE"
        $pwResult = Invoke-Plink -RemoteHost $gwHost -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $pwCmd

        if ($pwResult -match "PW_DONE") {
            Write-Host "  [$gwName] Password set" -ForegroundColor Green
        } else {
            Write-Host "  [$gwName] Password result: $($pwResult.Substring(0, [Math]::Min(200, $pwResult.Length)))" -ForegroundColor Yellow
        }

        # 7d: Assign RBA role (ignore error if already assigned)
        $null = Invoke-Plink -RemoteHost $gwHost -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command "clish -c 'add rba user $GAIA_MON_USER roles adminRole' 2>&1 || true"

        # 7e: Enable Gaia API access
        Write-Host "  [$gwName] Enabling Gaia API access..." -ForegroundColor White
        $gaiaCmd = "gaia_api access --user $GAIA_MON_USER --enable true 2>&1; echo GAIA_API_DONE"
        $gaiaResult = Invoke-Plink -RemoteHost $gwHost -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $gaiaCmd

        if ($gaiaResult -match "GAIA_API_DONE") {
            Write-Host "  [$gwName] Gaia API access enabled" -ForegroundColor Green
        } else {
            Write-Host "  [$gwName] Gaia API result: $($gaiaResult.Substring(0, [Math]::Min(200, $gaiaResult.Length)))" -ForegroundColor Yellow
        }

        # 7f: Save config and restore shell
        Write-Host "  [$gwName] Saving config and restoring shell..." -ForegroundColor White
        $null = Invoke-Plink -RemoteHost $gwHost -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command "clish -c 'save config' 2>&1"
        Set-ClishShell -RemoteHost $gwHost -User $GAIA_ADMIN -Password $LAB_PASSWORD
        Write-Host "  [$gwName] Done" -ForegroundColor Green

    } catch {
        Write-Host "  Warning: Gateway setup error on ${gwName}: $_" -ForegroundColor Red
        Write-Host "  Manual fix: SSH to $gwHost (see README)" -ForegroundColor Yellow
        try { Set-ClishShell -RemoteHost $gwHost -User $GAIA_ADMIN -Password $LAB_PASSWORD } catch {}
    }
}

# ---- Step 8: Create credentials.json and launch ----
Write-Host "[8/8] Creating credentials and launching..." -ForegroundColor Yellow

$credJson = '{"management":{"user":"' + $MONITOR_USER + '","password":"' + $MONITOR_PASS + '"},"gaia":{"user":"' + $GAIA_MON_USER + '","password":"' + $GAIA_MON_PASS + '"}}'
$credJson | Out-File -FilePath "$INSTALL_DIR\credentials.json" -Encoding ascii -NoNewline

try {
    $null = Get-Content "$INSTALL_DIR\credentials.json" -Raw | ConvertFrom-Json
    Write-Host "  credentials.json created and validated" -ForegroundColor Green
} catch {
    Write-Host "  Warning: credentials.json may be malformed: $_" -ForegroundColor Red
}

Write-Host ""
Write-Host "====================================" -ForegroundColor Green
Write-Host "  chkp-monitor installed!" -ForegroundColor Green
Write-Host "====================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Location:  $INSTALL_DIR" -ForegroundColor White
Write-Host "  Dashboard: http://localhost:8080" -ForegroundColor White
Write-Host ""
Write-Host "  Mgmt API user:  $MONITOR_USER" -ForegroundColor White
Write-Host "  Gaia API user:  $GAIA_MON_USER" -ForegroundColor White
Write-Host ""

if (-not $mgmtReady) {
    Write-Host "  NOTE: Management API may need manual verification." -ForegroundColor Yellow
    Write-Host "  SSH to A-SMS: api status" -ForegroundColor Yellow
    Write-Host ""
}

Write-Host "Starting dashboard..." -ForegroundColor Yellow
Write-Host "(Press Ctrl+C to stop)" -ForegroundColor Gray
Write-Host ""

Set-Location $INSTALL_DIR
& python server.py
