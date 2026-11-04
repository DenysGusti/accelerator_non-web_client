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

# 1.5 Configure Client for NFS (NfsClnt) service startup type to Manual
# This prevents NFS Client from starting automatically on Windows boot, avoiding startup delays/hangs.
# The service will be started on-demand when accli mounts a drive.
Write-Host "Configuring Client for NFS (NfsClnt) service startup type to Manual..." -ForegroundColor Cyan
if (Get-Service -Name "NfsClnt" -ErrorAction SilentlyContinue) {
    Set-Service -Name "NfsClnt" -StartupType Manual -ErrorAction SilentlyContinue
    Write-Host "[OK] NfsClnt service startup type configured to Manual." -ForegroundColor Green
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

# 2.6 Configure Client for NFS Cache (fast directory refresh without caching delay)
Write-Host "Configuring Client for NFS directory cache timeouts..." -ForegroundColor Cyan

# Global redirector controls:
# DisableCache = 0 allows file content caching in memory
New-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\ClientForNFS\CurrentVersion\Default" -Name "DisableCache" -Value 0 -PropertyType DWORD -Force | Out-Null
# MaxDirCacheSize = 0 bypasses directory tree RAM cache so newly created/deleted files appear immediately
New-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\ClientForNFS\CurrentVersion\Default" -Name "MaxDirCacheSize" -Value 0 -PropertyType DWORD -Force | Out-Null

$cacheTargets = @(
    "HKLM:\SOFTWARE\Microsoft\ClientForNFS\CurrentVersion\Users\Default\Cache"
)

$existingUsers = Get-ChildItem -Path "HKLM:\SOFTWARE\Microsoft\ClientForNFS\CurrentVersion\Users" -ErrorAction SilentlyContinue | 
    Where-Object { $_.PSChildName -like "S-1-5-*" }

foreach ($u in $existingUsers) {
    $cacheTargets += "$($u.PSPath)\Cache"
}

foreach ($targetPath in $cacheTargets) {
    if (-not (Test-Path $targetPath)) {
        New-Item -Path $targetPath -Force | Out-Null
    }
    # Enable the attribute cache
    New-ItemProperty -Path $targetPath -Name "FileAttributeCache" -Value 1 -PropertyType DWORD -Force | Out-Null
    # AttributeTimeDelta: metadata TTL (10s) to align with --metadata-ttl-ms 10000
    New-ItemProperty -Path $targetPath -Name "AttributeTimeDelta" -Value 10 -PropertyType DWORD -Force | Out-Null
    # CacheRefreshInterval: file content cache TTL (10s) so modified files refresh within 10s
    New-ItemProperty -Path $targetPath -Name "CacheRefreshInterval" -Value 10 -PropertyType DWORD -Force | Out-Null
}
Write-Host "[OK] Client for NFS balanced cache policy configured (10s TTL, No Dir Cache)." -ForegroundColor Green

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

# Ensure NfsRdr and NfsClnt services are started before attempting mount
try {
    if ((Get-Service -Name "NfsRdr" -ErrorAction SilentlyContinue).Status -ne 'Running') {
        Start-Service -Name "NfsRdr" -ErrorAction SilentlyContinue
    }
    if ((Get-Service -Name "NfsClnt" -ErrorAction SilentlyContinue).Status -ne 'Running') {
        Start-Service -Name "NfsClnt" -ErrorAction SilentlyContinue
    }
} catch {}

# Force unified, accessible cache under C:\ProgramData\accli\cache
$cacheDir = "C:\ProgramData\accli\cache"
$hfXetCache = "$cacheDir\xet"
$hfMountCache = "$cacheDir\hf-mount"
New-Item -ItemType Directory -Path $hfXetCache -Force | Out-Null
New-Item -ItemType Directory -Path $hfMountCache -Force | Out-Null

$env:HF_XET_CACHE = $hfXetCache
$env:HF_HOME = $cacheDir
$env:XDG_CACHE_HOME = $cacheDir

$mountArgs = @(
    "--token-file", $cfg.db_path,
    "--hub-endpoint", $cfg.server_url,
    "--cache-dir", $hfMountCache,
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

# Synchronously stop NfsClnt & NfsRdr, then start NfsRdr & NfsClnt to flush stale kernel V_NET_ROOT cache
try {
    Stop-Service -Name "NfsClnt" -Force -ErrorAction SilentlyContinue
    Stop-Service -Name "NfsRdr" -Force -ErrorAction SilentlyContinue
    Start-Service -Name "NfsRdr" -ErrorAction SilentlyContinue
    Start-Service -Name "NfsClnt" -ErrorAction SilentlyContinue
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
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
        
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
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
        
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
