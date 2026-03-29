<#
.SYNOPSIS
    chkp-monitor bootstrap script
    Run on a fresh Skillable lab A-GUI to install dependencies,
    configure API access, create monitoring accounts, and launch the health dashboard.

.USAGE
    irm https://raw.githubusercontent.com/Don-Paterson/chkp-monitor/main/bootstrap.ps1 | iex

.NOTES
    - mgmt_cli commands require domain "System Data" for admin operations
    - Gaia API access must be enabled per-user via expert mode
    - plink.exe is pre-installed at C:\Program Files\PuTTY\
    - All commands run non-interactively via plink -batch
#>

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"  # Speed up Invoke-WebRequest downloads

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

# Skip SSL cert validation for self-signed Check Point certs (PS7 compatible)
$PSDefaultParameterValues['Invoke-RestMethod:SkipCertificateCheck'] = $true
$PSDefaultParameterValues['Invoke-WebRequest:SkipCertificateCheck'] = $true

Write-Host ""
Write-Host "====================================" -ForegroundColor Cyan
Write-Host "  chkp-monitor bootstrap" -ForegroundColor Cyan
Write-Host "====================================" -ForegroundColor Cyan
Write-Host ""

# ---- Helper: run a single command on remote host via plink ----
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

# ---- Step 0: Accept SSH host keys for all hosts ----
Write-Host "[0/8] Accepting SSH host keys..." -ForegroundColor Yellow
foreach ($h in @($MGMT_HOST, $GW01_HOST, $GW02_HOST)) {
    # Pipe 'y' to auto-accept the host key on first connection
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

    # Refresh PATH for this session
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")

    Write-Host "  Python installed successfully" -ForegroundColor Green
}

# ---- Step 2: Install PowerShell 7 (optional, skip if already running in PS7) ----
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
# All mgmt_cli admin commands require domain "System Data" and a session.
# Flow: login with domain -> get SID -> run commands with -s SID -> publish -> logout
# We run these commands ON the A-SMS box via plink (local mgmt_cli, no -m flag needed).
Write-Host "[5/8] Configuring Management API on A-SMS ($MGMT_HOST)..." -ForegroundColor Yellow

try {
    # 5a: Login to mgmt_cli with domain "System Data", capture SID to file
    Write-Host "  Logging in to management API (domain: System Data)..." -ForegroundColor White
    $loginCmd = "mgmt_cli login user $MGMT_ADMIN password '$LAB_PASSWORD' domain ""System Data"" > /tmp/chkp-mon-sid.txt 2>&1 && echo LOGIN_OK || echo LOGIN_FAIL"
    $loginResult = Invoke-Plink -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $loginCmd

    if ($loginResult -match "LOGIN_OK") {
        Write-Host "  Logged in successfully" -ForegroundColor Green

        # 5b: Create monitor-api administrator (read-only)
        Write-Host "  Creating $MONITOR_USER administrator..." -ForegroundColor White
        $addCmd = "mgmt_cli add administrator name ""$MONITOR_USER"" password ""$MONITOR_PASS"" must-change-password false permissions-profile ""Read Only All"" -s /tmp/chkp-mon-sid.txt 2>&1"
        $addResult = Invoke-Plink -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $addCmd

        if ($addResult -match "already exists") {
            Write-Host "  $MONITOR_USER already exists" -ForegroundColor Green
        } elseif ($addResult -match "uid") {
            Write-Host "  $MONITOR_USER created" -ForegroundColor Green
        } else {
            Write-Host "  add-administrator result: $($addResult.Substring(0, [Math]::Min(200, $addResult.Length)))" -ForegroundColor Yellow
        }

        # 5c: Open management API to all IP addresses
        Write-Host "  Setting API access to all IP addresses..." -ForegroundColor White
        $apiCmd = "mgmt_cli set api-settings accepted-api-calls-from ""All IP addresses"" -s /tmp/chkp-mon-sid.txt 2>&1"
        $apiResult = Invoke-Plink -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $apiCmd
        Write-Host "  API access configured" -ForegroundColor Green

        # 5d: Publish changes
        Write-Host "  Publishing..." -ForegroundColor White
        $pubCmd = "mgmt_cli publish -s /tmp/chkp-mon-sid.txt 2>&1"
        $pubResult = Invoke-Plink -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $pubCmd

        if ($pubResult -match "succeeded") {
            Write-Host "  Published successfully" -ForegroundColor Green
        } else {
            Write-Host "  Publish result: $($pubResult.Substring(0, [Math]::Min(200, $pubResult.Length)))" -ForegroundColor Yellow
        }

        # 5e: Logout and clean up SID file
        $logoutCmd = "mgmt_cli logout -s /tmp/chkp-mon-sid.txt 2>&1; rm -f /tmp/chkp-mon-sid.txt"
        $null = Invoke-Plink -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $logoutCmd

        # 5f: Restart API to apply the accepted-api-calls-from change
        Write-Host "  Restarting management API..." -ForegroundColor White
        $restartCmd = "api restart 2>&1"
        $restartResult = Invoke-Plink -RemoteHost $MGMT_HOST -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $restartCmd

        if ($restartResult -match "started successfully") {
            Write-Host "  Management API restarted successfully" -ForegroundColor Green
        } else {
            Write-Host "  API restart output: $($restartResult.Substring(0, [Math]::Min(200, $restartResult.Length)))" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  Login failed: $($loginResult.Substring(0, [Math]::Min(200, $loginResult.Length)))" -ForegroundColor Red
        Write-Host "  You will need to configure the management API manually" -ForegroundColor Yellow
    }

} catch {
    Write-Host "  Warning: Management API setup error: $_" -ForegroundColor Red
    Write-Host "  Manual fix: SSH to A-SMS and run mgmt_cli commands (see README)" -ForegroundColor Yellow
}

# ---- Step 6: Verify Management API is accepting remote connections ----
Write-Host "[6/8] Verifying Management API accepts remote connections..." -ForegroundColor Yellow

$mgmtReady = $false
for ($i = 1; $i -le 12; $i++) {
    try {
        $testBody = @{ user = $MONITOR_USER; password = $MONITOR_PASS } | ConvertTo-Json
        $testResp = Invoke-RestMethod -Uri "https://${MGMT_HOST}/web_api/login" -Method Post `
            -Body $testBody -ContentType "application/json" -TimeoutSec 5
        if ($testResp.sid) {
            # Logout cleanly
            $logoutHeaders = @{ "X-chkp-sid" = $testResp.sid }
            $null = Invoke-RestMethod -Uri "https://${MGMT_HOST}/web_api/logout" -Method Post `
                -Body "{}" -ContentType "application/json" -Headers $logoutHeaders -TimeoutSec 5
            $mgmtReady = $true
            break
        }
    } catch {}
    Write-Host "  Waiting for API... ($i/12)" -ForegroundColor White
    Start-Sleep -Seconds 5
}

if ($mgmtReady) {
    Write-Host "  Management API is ready and accepting remote connections" -ForegroundColor Green
} else {
    Write-Host "  Management API not responding yet - dashboard will keep retrying" -ForegroundColor Yellow
    Write-Host "  If this persists, verify 'api status' on A-SMS shows 'All IP Addresses'" -ForegroundColor Yellow
}

# ---- Step 7: Create Gaia API users on gateways ----
# Each gateway needs: user created in Clish, password set, RBA role assigned,
# Gaia API access enabled in expert mode, config saved.
# All done non-interactively via plink using a single compound command.
Write-Host "[7/8] Configuring Gaia API users on gateways..." -ForegroundColor Yellow

foreach ($gwHost in @($GW01_HOST, $GW02_HOST)) {
    $gwName = if ($gwHost -eq $GW01_HOST) { "A-GW-01" } else { "A-GW-02" }

    try {
        # 7a: Create user, set password hash, assign RBA role, save config - all in Clish
        # Using password-hash with openssl avoids the interactive password prompt
        Write-Host "  [$gwName] Creating user and configuring Clish..." -ForegroundColor White
        $clishCmd = @(
            "add user $GAIA_MON_USER uid 0 homedir /home/$GAIA_MON_USER 2>&1 || true",
            "set user $GAIA_MON_USER password-hash `$(openssl passwd -6 '$LAB_PASSWORD') 2>&1",
            "add rba user $GAIA_MON_USER roles adminRole 2>&1 || true",
            "save config 2>&1"
        ) -join " ; "

        $clishResult = Invoke-Plink -RemoteHost $gwHost -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $clishCmd

        if ($clishResult -match "already exists") {
            Write-Host "  [$gwName] User already exists, continuing..." -ForegroundColor Green
        } else {
            Write-Host "  [$gwName] User created and config saved" -ForegroundColor Green
        }

        # 7b: Enable Gaia API access in expert mode
        # Use a single bash command string that enters expert, runs the command, and exits
        Write-Host "  [$gwName] Enabling Gaia API access (expert mode)..." -ForegroundColor White
        $expertCmd = "bash -c 'echo ""$LAB_PASSWORD"" | /bin/cpshell -s /opt/CPshrd-R82/bin/cpshell_scripts/expert_mode.sh && gaia_api access --user $GAIA_MON_USER --enable true 2>&1 || echo EXPERT_FAILED'"

        # Simpler approach: use plink to run expert commands via expect-like stdin
        # Actually, the most reliable non-interactive way is clish -c for clish commands
        # and for expert mode, use the CLISH expert command path
        $expertCmd2 = "clish -s -c ""lock database override"" 2>&1; echo '$LAB_PASSWORD' | expert -c ""gaia_api access --user $GAIA_MON_USER --enable true"" 2>&1 || true"
        $expertResult = Invoke-Plink -RemoteHost $gwHost -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $expertCmd2

        # If that approach failed, try a different path
        if ($expertResult -match "EXPERT_FAILED" -or $expertResult -match "command not found") {
            Write-Host "  [$gwName] Trying alternative expert mode approach..." -ForegroundColor White
            # Use script command to fake a tty, or just run as bash
            $altCmd = "bash -lc 'source /opt/CPshrd-R82/tmp/.CPprofile.sh 2>/dev/null; gaia_api access --user $GAIA_MON_USER --enable true 2>&1'"
            $altResult = Invoke-Plink -RemoteHost $gwHost -User $GAIA_ADMIN -Password $LAB_PASSWORD -Command $altCmd
            Write-Host "  [$gwName] Alternative result: $($altResult.Substring(0, [Math]::Min(150, $altResult.Length)))" -ForegroundColor Yellow
        } else {
            Write-Host "  [$gwName] Gaia API access enabled" -ForegroundColor Green
        }

    } catch {
        Write-Host "  Warning: Gateway setup error on ${gwName}: $_" -ForegroundColor Red
        Write-Host "  Manual fix needed on $gwHost (see README for commands)" -ForegroundColor Yellow
    }
}

# ---- Step 8: Create credentials.json and launch ----
Write-Host "[8/8] Creating credentials and launching..." -ForegroundColor Yellow

# Write credentials.json using Out-File with ASCII to avoid BOM issues
$credJson = '{"management":{"user":"' + $MONITOR_USER + '","password":"' + $MONITOR_PASS + '"},"gaia":{"user":"' + $GAIA_MON_USER + '","password":"' + $LAB_PASSWORD + '"}}'
$credJson | Out-File -FilePath "$INSTALL_DIR\credentials.json" -Encoding ascii -NoNewline

# Verify the JSON is valid
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

# ---- Check if any steps need manual intervention ----
if (-not $mgmtReady) {
    Write-Host "  NOTE: Management API may need manual configuration." -ForegroundColor Yellow
    Write-Host "  SSH to A-SMS and verify: api status" -ForegroundColor Yellow
    Write-Host ""
}

Write-Host "Starting dashboard..." -ForegroundColor Yellow
Write-Host "(Press Ctrl+C to stop)" -ForegroundColor Gray
Write-Host ""

# Launch the server
Set-Location $INSTALL_DIR
& python server.py
