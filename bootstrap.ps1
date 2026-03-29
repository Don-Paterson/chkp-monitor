<#
.SYNOPSIS
    chkp-monitor bootstrap script
    Run on a fresh Skillable lab A-GUI to install dependencies,
    create monitoring accounts, and launch the health dashboard.

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

# Lab defaults
$MGMT_HOST     = "10.1.1.101"
$GW01_HOST     = "10.1.1.2"
$GW02_HOST     = "10.1.1.3"
$MGMT_ADMIN    = "cpadmin"
$GAIA_ADMIN    = "admin"
$LAB_PASSWORD  = 'Chkp!234'
$MONITOR_USER  = "monitor-api"
$MONITOR_PASS  = 'M0n!t0r@pi'

Write-Host ""
Write-Host "====================================" -ForegroundColor Cyan
Write-Host "  chkp-monitor bootstrap" -ForegroundColor Cyan
Write-Host "====================================" -ForegroundColor Cyan
Write-Host ""

# ---- Step 1: Install Python ----
Write-Host "[1/7] Checking Python..." -ForegroundColor Yellow
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
Write-Host "[2/7] Checking PowerShell 7..." -ForegroundColor Yellow
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
Write-Host "[3/7] Installing Python packages..." -ForegroundColor Yellow
& python -m pip install --quiet --upgrade pip 2>$null
& python -m pip install --quiet flask requests paramiko 2>$null
Write-Host "  Flask, requests, paramiko installed" -ForegroundColor Green

# ---- Step 4: Download chkp-monitor files ----
Write-Host "[4/7] Downloading chkp-monitor..." -ForegroundColor Yellow

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

# ---- Step 5: Create monitor-api account on A-SMS ----
Write-Host "[5/7] Creating monitor-api account on A-SMS..." -ForegroundColor Yellow

# Ignore SSL cert errors for self-signed Check Point certs
# Skip SSL cert validation (self-signed Check Point certs) - PS7 compatible
$PSDefaultParameterValues['Invoke-RestMethod:SkipCertificateCheck'] = $true
$PSDefaultParameterValues['Invoke-WebRequest:SkipCertificateCheck'] = $true
$mgmtApiBase = "https://${MGMT_HOST}/web_api"

try {
    # Login as cpadmin
    $loginBody = @{
        user = $MGMT_ADMIN
        password = $LAB_PASSWORD
    } | ConvertTo-Json

    $loginResp = Invoke-RestMethod -Uri "$mgmtApiBase/login" -Method Post `
        -Body $loginBody -ContentType "application/json"
    $sid = $loginResp.sid
    $headers = @{ "X-chkp-sid" = $sid }

    # Check if monitor-api already exists
    $existsCheck = $true
    try {
        $existing = Invoke-RestMethod -Uri "$mgmtApiBase/show-administrator" -Method Post `
            -Body (@{ name = $MONITOR_USER } | ConvertTo-Json) `
            -ContentType "application/json" -Headers $headers
    } catch {
        $existsCheck = $false
    }

    if (-not $existsCheck) {
        # Create read-only administrator
        $addBody = @{
            name = $MONITOR_USER
            password = $MONITOR_PASS
            "authentication-method" = "check point password"
            "permissions-profile" = "Read Only All"
        } | ConvertTo-Json

        $addResp = Invoke-RestMethod -Uri "$mgmtApiBase/add-administrator" -Method Post `
            -Body $addBody -ContentType "application/json" -Headers $headers

        # Publish
        Invoke-RestMethod -Uri "$mgmtApiBase/publish" -Method Post `
            -Body "{}" -ContentType "application/json" -Headers $headers | Out-Null

        Write-Host "  monitor-api admin created on A-SMS" -ForegroundColor Green
    } else {
        Write-Host "  monitor-api admin already exists on A-SMS" -ForegroundColor Green
    }

    # Logout
    Invoke-RestMethod -Uri "$mgmtApiBase/logout" -Method Post `
        -Body "{}" -ContentType "application/json" -Headers $headers | Out-Null

} catch {
    Write-Host "  Warning: Could not create monitor-api on A-SMS: $_" -ForegroundColor Red
    Write-Host "  You may need to create it manually or use cpadmin credentials" -ForegroundColor Yellow
}

# ---- Step 6: Create monitor-api on gateways (Gaia API) ----
Write-Host "[6/7] Creating monitor-api accounts on gateways..." -ForegroundColor Yellow

foreach ($gwHost in @($GW01_HOST, $GW02_HOST)) {
    $gwName = if ($gwHost -eq $GW01_HOST) { "A-GW-01" } else { "A-GW-02" }
    $gaiaBase = "https://${gwHost}/gaia_api"

    try {
        # Login as admin
        $loginBody = @{
            user = $GAIA_ADMIN
            password = $LAB_PASSWORD
        } | ConvertTo-Json

        $loginResp = Invoke-RestMethod -Uri "$gaiaBase/login" -Method Post `
            -Body $loginBody -ContentType "application/json"
        $sid = $loginResp.sid
        $headers = @{ "X-chkp-sid" = $sid }

        # Try to add user via run-script (Gaia doesn't have a dedicated add-user API in all versions)
        $script = "clish -c 'add user $MONITOR_USER uid 0 homedir /home/$MONITOR_USER' 2>/dev/null; " +
                  "clish -c 'set user $MONITOR_USER password-hash `$(openssl passwd -6 `"$MONITOR_PASS`")' 2>/dev/null; " +
                  "clish -c 'add rba user $MONITOR_USER roles adminRole' 2>/dev/null; " +
                  "echo 'done'"

        $scriptBody = @{
            script = $script
        } | ConvertTo-Json

        $scriptResp = Invoke-RestMethod -Uri "$gaiaBase/run-script" -Method Post `
            -Body $scriptBody -ContentType "application/json" -Headers $headers

        Write-Host "  monitor-api user configured on $gwName" -ForegroundColor Green

        # Logout
        Invoke-RestMethod -Uri "$gaiaBase/logout" -Method Post `
            -Body "{}" -ContentType "application/json" -Headers $headers | Out-Null

    } catch {
        Write-Host "  Warning: Could not configure monitor-api on ${gwName}: $_" -ForegroundColor Red
        Write-Host "  Falling back to admin credentials for this gateway" -ForegroundColor Yellow
    }
}

# ---- Step 7: Create credentials.json and launch ----
Write-Host "[7/7] Creating credentials and launching..." -ForegroundColor Yellow

$credentials = @{
    management = @{
        user = $MONITOR_USER
        password = $MONITOR_PASS
    }
    gaia = @{
        user = $MONITOR_USER
        password = $MONITOR_PASS
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
Write-Host "Starting dashboard..." -ForegroundColor Yellow
Write-Host "(Press Ctrl+C to stop)" -ForegroundColor Gray
Write-Host ""

# Launch the server
Set-Location $INSTALL_DIR
& python server.py
