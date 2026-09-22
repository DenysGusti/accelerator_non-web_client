param(
    [Parameter(Position=0)]
    [string]$MountPoint,
    [Parameter(Position=1)]
    [ValidateSet("ListOpenPaths", "Refresh")]
    [string]$Action,
    [Parameter(Position=2)]
    [string]$TargetFolder
)

$ErrorActionPreference = "SilentlyContinue"
$mountClean = $MountPoint.TrimEnd("\/")

$shell = New-Object -ComObject Shell.Application

if ($Action -eq "ListOpenPaths") {
    $foundPaths = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($win in $shell.Windows()) {
        try {
            $p = $win.Document.Folder.Self.Path
            if (-not $p) {
                $u = [System.Uri]$win.LocationURL
                if ($u.IsFile) { $p = $u.LocalPath }
            }
            if ($p) {
                $pClean = $p.TrimEnd("\/")
                if ($pClean -eq $mountClean -or $pClean.StartsWith($mountClean + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
                    [void]$foundPaths.Add($pClean)
                }
            }
        } catch {}
    }
    foreach ($path in $foundPaths) {
        Write-Output $path
    }
}
else {
    $targetClean = if ($TargetFolder) { $TargetFolder.TrimEnd("\/") } else { "" }
    foreach ($win in $shell.Windows()) {
        try {
            $winPath = $win.Document.Folder.Self.Path
            if (-not $winPath) {
                $u = [System.Uri]$win.LocationURL
                if ($u.IsFile) { $winPath = $u.LocalPath }
            }
            if ($winPath) {
                $winClean = $winPath.TrimEnd("\/")
                if ($targetClean) {
                    if ($winClean -eq $targetClean -or $winClean.StartsWith($targetClean + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
                        $win.Refresh()
                    }
                } else {
                    if ($winClean -eq $mountClean -or $winClean.StartsWith($mountClean + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
                        $win.Refresh()
                    }
                }
            }
        } catch {}
    }
}
