<#
.SYNOPSIS
    One-time administrative configuration script to enable Windows Client for NFS and register the Task Scheduler Mount Gateway.
.DESCRIPTION
    This script performs the administrative setup required to allow standard users to mount 
    Accelerator project spaces on Windows over NFS with ZERO UAC prompts.
    It can be run silently by IT Administrators using Microsoft Intune, Group Policy (GPO), or SCCM.
.PARAMETER NfsAlreadyEnabled
    Bypasses enabling NFS Optional features if already enabled.
.PARAMETER RegAlreadyEnabled
    Bypasses EnableLinkedConnections registry policy creation if already enabled.
.NOTES
    This script is static and fully auditable by Security and Network Administrators.
#>

param (
    [switch]$NfsAlreadyEnabled,
    [switch]$RegAlreadyEnabled
)

# 1. Enable Windows Client for NFS Optional Features
if (-not $NfsAlreadyEnabled) {
    Write-Host "Enabling Windows Client for NFS features..." -ForegroundColor Cyan

    if (Get-Command Enable-WindowsOptionalFeature -ErrorAction SilentlyContinue) {
        # Split into two separate calls to bypass Windows 11 comma-separated feature parser bug
        Enable-WindowsOptionalFeature -Online -FeatureName ServicesForNFS-ClientOnly -All -NoRestart | Out-Null
        Enable-WindowsOptionalFeature -Online -FeatureName ClientForNFS-Infrastructure -All -NoRestart | Out-Null
    } elseif (Get-Command Install-WindowsFeature -ErrorAction SilentlyContinue) {
        # Fallback for Windows Server OS
        Install-WindowsFeature -Name NFS-Client | Out-Null
    }
    Write-Host "[OK] Enabled Client for NFS features." -ForegroundColor Green
} else {
    Write-Host "[OK] Client for NFS features already enabled." -ForegroundColor Green
}

# 1.5 Configure Client for NFS (NfsClnt) service startup type to Automatic (Delayed Start)
# This prevents NFS Client from scanning the network immediately on startup, which can cause extremely long boot times.
Write-Host "Configuring Client for NFS (NfsClnt) service startup type to Automatic (Delayed Start)..." -ForegroundColor Cyan
if (Get-Service -Name "NfsClnt" -ErrorAction SilentlyContinue) {
    if ($PSVersionTable.PSVersion.Major -lt 6) {
        Write-Host "Windows PowerShell (5.1 or older) detected. PowerShell 7+ is required to configure Automatic (Delayed Start)." -ForegroundColor Yellow
        
        # Check if pwsh is already installed
        $pwshPath = "$env:ProgramFiles\PowerShell\7\pwsh.exe"
        if (-not (Test-Path $pwshPath)) {
            $pwshPath = "pwsh.exe"
            $hasPwsh = Get-Command $pwshPath -ErrorAction SilentlyContinue
        } else {
            $hasPwsh = $true
        }

        if (-not $hasPwsh) {
            Write-Host "PowerShell 7 is not installed. Downloading and installing silently..." -ForegroundColor Yellow
            try {
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                $installScript = Invoke-RestMethod -Uri "https://aka.ms/install-powershell.ps1"
                $scriptBlock = [ScriptBlock]::Create($installScript)
                # Run Microsoft's installer silently using the call operator to bind switches correctly
                & $scriptBlock -UseMSI -Quiet
                Write-Host "[OK] PowerShell 7 installed successfully." -ForegroundColor Green
            } catch {
                Write-Error "Failed to install PowerShell 7: $_"
                exit 1
            }
            
            # Re-check standard path after installation
            $pwshPath = "$env:ProgramFiles\PowerShell\7\pwsh.exe"
            if (-not (Test-Path $pwshPath)) {
                Write-Error "Could not find pwsh.exe after installation."
                exit 1
            }
        } else {
            if ($pwshPath -eq "pwsh.exe") {
                $pwshPath = (Get-Command pwsh.exe).Source
            }
        }

        Write-Host "Restarting script under PowerShell 7..." -ForegroundColor Cyan
        $repassArgs = @()
        if ($NfsAlreadyEnabled) { $repassArgs += "-NfsAlreadyEnabled" }
        if ($RegAlreadyEnabled) { $repassArgs += "-RegAlreadyEnabled" }
        & $pwshPath -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath @repassArgs
        exit
    }

    Set-Service -Name "NfsClnt" -StartupType AutomaticDelayedStart -ErrorAction SilentlyContinue
    Write-Host "[OK] NfsClnt service startup type configured to Automatic (Delayed Start)." -ForegroundColor Green
} else {
    Write-Warning "Client for NFS service (NfsClnt) not found. Could not configure startup type."
}

# 2. Add Registry value for Linked Connections
# Allows mapped network drives to be shared between elevated and standard sessions under the same user log-in.
if (-not $RegAlreadyEnabled) {
    Write-Host "Configuring EnableLinkedConnections registry policy..." -ForegroundColor Cyan

    $regPath = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
    if (-not (Test-Path $regPath)) {
        New-Item -Path $regPath -Force | Out-Null
    }
    New-ItemProperty -Path $regPath -Name "EnableLinkedConnections" -Value 1 -PropertyType DWORD -Force | Out-Null
    Write-Host "[OK] Registry value configured." -ForegroundColor Green
} else {
    Write-Host "[OK] Registry value already configured." -ForegroundColor Green
}

# 2.5 Configure Anonymous UID/GID to match hf-mount-nfs (UID 0)
if (-not $RegAlreadyEnabled) {
    Write-Host "Configuring Anonymous UID/GID for NFS Client..." -ForegroundColor Cyan
    $clientForNfsPath = "HKLM:\SOFTWARE\Microsoft\ClientForNFS\CurrentVersion\Default"
    if (-not (Test-Path $clientForNfsPath)) {
        New-Item -Path $clientForNfsPath -Force | Out-Null
    }
    New-ItemProperty -Path $clientForNfsPath -Name "AnonymousUid" -Value 0 -PropertyType DWORD -Force | Out-Null
    New-ItemProperty -Path $clientForNfsPath -Name "AnonymousGid" -Value 0 -PropertyType DWORD -Force | Out-Null
    Write-Host "[OK] Anonymous UID/GID configured." -ForegroundColor Green
} else {
    Write-Host "[OK] Anonymous UID/GID registry values already configured (skipped)." -ForegroundColor Green
}

# 3. Create public ProgramData folder and configure permissions for Authenticated Users
Write-Host "Initializing public accli configuration directory..." -ForegroundColor Cyan
$configDir = "C:\ProgramData\accli"
if (-not (Test-Path $configDir)) {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
}

try {
    $acl = Get-Acl $configDir
    # Grant Modify (Read, Write, Execute, Delete) to all Authenticated Users so non-admin users can write the mount_config.json
    $ar = New-Object System.Security.AccessControl.FileSystemAccessRule("Authenticated Users", "Modify", "ContainerInherit,ObjectInherit", "None", "Allow")
    $acl.SetAccessRule($ar)
    Set-Acl $configDir $acl
    Write-Host "[OK] Directory permissions configured." -ForegroundColor Green
} catch {
    Write-Warning "Could not configure ProgramData permissions. Standalone non-admin users might not be able to write configuration."
}

# 4. Write static run-mount-helper.ps1 static helper
Write-Host "Registering run-mount-helper script..." -ForegroundColor Cyan
$helperScript = @'
$configPath = "C:\ProgramData\accli\mount_config.json"
if (-not (Test-Path $configPath)) {
    Write-Error "Config file not found at $configPath."
    exit 1
}

try {
    $cfg = Get-Content $configPath -Raw | ConvertFrom-Json
} catch {
    Write-Error "Failed to parse $configPath."
    exit 1
}

# Construct environment
$env:PATH = "C:\Windows\System32\downlevel;" + $env:PATH
$env:ACCELERATOR_MOUNT = "1"
$env:ACC_ENDPOINT = $cfg.server_url
$env:ACC_CAS_ENDPOINT = "$($cfg.server_url.TrimEnd('/'))/api/xet-cas"
$env:ACC_TOKEN = $cfg.token
if ($cfg.skip_auto_mount) {
    $env:HF_MOUNT_SKIP_AUTO_MOUNT = "1"
}

# Construct arguments
$mountArgs = @(
    "--token-file", $cfg.db_path,
    "--hub-endpoint", $cfg.server_url,
    $cfg.mode,
    $cfg.project_slug,
    $cfg.mount_point
)

if ($cfg.overlay) { $mountArgs += "--overlay" }
if ($cfg.read_only) { $mountArgs += "--read-only" }

# Prepare background logger
$logFile = "C:\ProgramData\accli\mount.log"
$errFile = "C:\ProgramData\accli\mount_err.log"
try {
    # Run the background daemon process elevated (since task runs as SYSTEM)
    & $cfg.hf_mount_bin @mountArgs > $logFile 2> $errFile
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne $null) {
        Add-Content -Path $errFile -Value "Process exited with code `$LASTEXITCODE"
        exit `$LASTEXITCODE
    }
} catch {
    Write-Error "Failed to execute hf-mount-nfs.exe."
    exit 1
}
'@

$helperScriptPath = "C:\ProgramData\accli\run-mount-helper.ps1"
Set-Content -Path $helperScriptPath -Value $helperScript -Encoding UTF8
Write-Host "[OK] Mount helper script registered." -ForegroundColor Green

# 4.5 Write static run-umount-helper.ps1 static helper
Write-Host "Registering run-umount-helper script..." -ForegroundColor Cyan
$helperUmountScript = @'
try {
    if (Get-Command Stop-ScheduledTask -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName "accli-mount-nfs" -ErrorAction SilentlyContinue | Out-Null
    } else {
        schtasks /end /tn "accli-mount-nfs" 2> $null | Out-Null
    }
} catch {}

try {
    Get-CimInstance Win32_Process -Filter "name='hf-mount-nfs.exe'" | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue | Out-Null
    }
} catch {}
'@

$helperUmountScriptPath = "C:\ProgramData\accli\run-umount-helper.ps1"
Set-Content -Path $helperUmountScriptPath -Value $helperUmountScript -Encoding UTF8
Write-Host "[OK] Umount helper script registered." -ForegroundColor Green

# 5. Register the Scheduled Task (accli-mount-nfs)
Write-Host "Registering accli-mount-nfs Scheduled Task..." -ForegroundColor Cyan
$taskName = "accli-mount-nfs"

# Helper block executing PowerShell in Task Action
$psActionPath = "powershell.exe"
$psActionArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$helperScriptPath`""

if (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue) {
    try {
        $action = New-ScheduledTaskAction -Execute $psActionPath -Argument $psActionArgs
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        
        # Unregister task if already exists to update
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
        
        Write-Host "[OK] Scheduled Task registered successfully." -ForegroundColor Green
    } catch {
        # Fallback to schtasks.exe if cmdlet fails
        schtasks /delete /tn $taskName /f 2> $null | Out-Null
        schtasks /create /tn $taskName /tr "$psActionPath $psActionArgs" /sc ONCE /sd "2026/01/01" /st "00:00" /ru "SYSTEM" /rl "HIGHEST" /f | Out-Null
        Write-Host "[OK] Scheduled Task registered successfully." -ForegroundColor Green
    }
} else {
    # Legacy fallback
    schtasks /delete /tn $taskName /f 2> $null | Out-Null
    schtasks /create /tn $taskName /tr "$psActionPath $psActionArgs" /sc ONCE /sd "2026/01/01" /st "00:00" /ru "SYSTEM" /rl "HIGHEST" /f | Out-Null
    Write-Host "[OK] Scheduled Task registered successfully." -ForegroundColor Green
}

# 5.5 Register the Scheduled Task (accli-umount-nfs)
Write-Host "Registering accli-umount-nfs Scheduled Task..." -ForegroundColor Cyan
$umountTaskName = "accli-umount-nfs"
$psUmountActionArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$helperUmountScriptPath`""

if (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue) {
    try {
        $action = New-ScheduledTaskAction -Execute $psActionPath -Argument $psUmountActionArgs
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        
        Unregister-ScheduledTask -TaskName $umountTaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        Register-ScheduledTask -TaskName $umountTaskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
        
        Write-Host "[OK] Umount Scheduled Task registered successfully." -ForegroundColor Green
    } catch {
        schtasks /delete /tn $umountTaskName /f 2> $null | Out-Null
        schtasks /create /tn $umountTaskName /tr "$psActionPath $psUmountActionArgs" /sc ONCE /sd "2026/01/01" /st "00:00" /ru "SYSTEM" /rl "HIGHEST" /f | Out-Null
        Write-Host "[OK] Umount Scheduled Task registered successfully." -ForegroundColor Green
    }
} else {
    schtasks /delete /tn $umountTaskName /f 2> $null | Out-Null
    schtasks /create /tn $umountTaskName /tr "$psActionPath $psUmountActionArgs" /sc ONCE /sd "2026/01/01" /st "00:00" /ru "SYSTEM" /rl "HIGHEST" /f | Out-Null
    Write-Host "[OK] Umount Scheduled Task registered successfully." -ForegroundColor Green
}

# Grant Authenticated Users permission to read, query, and run the task files
foreach ($tName in @("accli-mount-nfs", "accli-umount-nfs")) {
    $taskFile = "C:\Windows\System32\Tasks\$tName"
    if (Test-Path $taskFile) {
        try {
            $acl = Get-Acl $taskFile
            $rule = New-Object System.Security.AccessControl.FileSystemAccessRule("Authenticated Users", "ReadAndExecute", "Allow")
            $acl.AddAccessRule($rule)
            Set-Acl $taskFile $acl
            Write-Host "[OK] Task file permissions configured for $tName." -ForegroundColor Green
        } catch {
            Write-Warning "Could not configure task file permissions for $tName."
        }
    }
}

# Grant Authenticated Users permission to query and trigger the Task Scheduler Service task objects themselves via COM SDDL
try {
    $Scheduler = New-Object -ComObject "Schedule.Service"
    $Scheduler.Connect()
    
    foreach ($tName in @("accli-mount-nfs", "accli-umount-nfs")) {
        $TaskObject = $Scheduler.GetFolder("\").GetTask($tName)
        $SDDL = $TaskObject.GetSecurityDescriptor(0xF)
        
        # (A;;GRGX;;;AU) grants Read (GR) and Execute (GX) to Authenticated Users (AU)
        if ($SDDL -notmatch 'A;;0x1200a9;;;AU' -and $SDDL -notmatch 'A;;GRGX;;;AU') {
            $SDDL = $SDDL + '(A;;GRGX;;;AU)'
            $TaskObject.SetSecurityDescriptor($SDDL, 0)
            Write-Host "[OK] Task Scheduler COM Service permissions configured for $tName." -ForegroundColor Green
        } else {
            Write-Host "[OK] Task Scheduler COM Service permissions already configured for $tName." -ForegroundColor Green
        }
    }
} catch {
    Write-Warning "Could not configure Task Scheduler COM permissions: $_"
}

Write-Host "Windows NFS Client Task Scheduler Gateway Setup Completed Successfully!" -ForegroundColor Green
Write-Host "⚠ Note: A Windows sign-out/sign-in or system restart is required for changes to take effect." -ForegroundColor Yellow
