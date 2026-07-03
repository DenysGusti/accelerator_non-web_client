import os
import platform
import re
import shutil
import stat
from pathlib import Path
import requests
import typer
from rich import print
from rich.progress import Progress, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn

DEFAULT_VERSION = "v0.6.5-acc-pr140-p4"
BINARY_DIR = Path.home() / ".accli" / "bin"

# Regex to safely validate version strings to prevent URL manipulation or directory traversal
VERSION_REGEX = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9_\-\.]+)?$")

def get_platform_suffix():
    """Detects the current OS and CPU architecture and maps them to the release asset suffix."""
    sys_name = platform.system()
    machine = platform.machine().lower()

    if sys_name == "Linux":
        if "aarch64" in machine or "arm64" in machine:
            return "aarch64-linux"
        else:
            return "x86_64-linux"
    elif sys_name == "Darwin":
        if "arm64" in machine or "aarch64" in machine:
            return "arm64-apple-darwin"
        else:
            return "x86_64-apple-darwin"
    elif sys_name == "Windows":
        return "x86_64-windows"
    else:
        # Fallback or unsupported platform
        raise typer.BadParameter(f"Unsupported platform: {sys_name} ({machine})")

def get_binary_path(binary_name: str) -> Path:
    """Returns the local absolute path where the binary should be cached."""
    # Ensure binary_name is strictly an allowed filename
    if binary_name not in ["hf-mount", "hf-mount-nfs", "hf-mount-fuse"]:
        raise ValueError("Invalid binary name requested")
    if platform.system() == "Windows":
        return BINARY_DIR / f"{binary_name}.exe"
    return BINARY_DIR / binary_name

def is_binary_available(binary_name: str, version: str = DEFAULT_VERSION) -> bool:
    """Checks if a binary is available in the custom accli cache or the system PATH."""
    local_path = get_binary_path(binary_name)
    version_file = BINARY_DIR / ".version"
    
    if local_path.is_file():
        cached_version = None
        if version_file.is_file():
            try:
                cached_version = version_file.read_text().strip()
            except Exception:
                pass
        
        if cached_version == version:
            if platform.system() == "Windows":
                return True
            elif os.access(local_path, os.X_OK):
                return True
        else:
            try:
                local_path.unlink()
            except Exception:
                pass
    
    # Also check if it exists in system PATH
    system_path = shutil.which(binary_name)
    if system_path:
        return True
        
    return False

def ensure_binaries(version: str = DEFAULT_VERSION, use_fuse: bool = False):
    """Ensures hf-mount and the required backend binary (NFS or FUSE) are downloaded and cached."""
    if not VERSION_REGEX.match(version):
        raise typer.BadParameter("Invalid version string format. Must be like v0.6.1-acc-p1-pr140")

    sys_name = platform.system()
    if sys_name == "Windows":
        required_binaries = ["hf-mount-nfs"]
    else:
        required_binaries = ["hf-mount"]
        if use_fuse:
            required_binaries.append("hf-mount-fuse")
        else:
            required_binaries.append("hf-mount-nfs")

    # Filter out already available binaries
    to_download = [bin_name for bin_name in required_binaries if not is_binary_available(bin_name, version)]

    if not to_download:
        return

    # Create binary cache directory securely
    BINARY_DIR.mkdir(parents=True, exist_ok=True)
    suffix = get_platform_suffix()

    print(f"[bold cyan]Downloading required mount binaries ({version})...[/bold cyan]")
    
    for bin_name in to_download:
        asset_name = f"{bin_name}-{suffix}"
        if sys_name == "Windows":
            asset_name += ".exe"
        url = f"https://github.com/Wrufesh/hf-mount/releases/download/{version}/{asset_name}"
        dest_path = get_binary_path(bin_name)
        part_path = dest_path.with_suffix(".part")

        print(f"[cyan]Downloading {bin_name} -> {dest_path}...[/cyan]")
        
        try:
            response = requests.get(url, stream=True, timeout=30)
            response.raise_for_status()
            total_size = int(response.headers.get("content-length", 0))

            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                transient=True,
            ) as progress:
                task = progress.add_task(f"Downloading {bin_name}", total=total_size)
                
                with open(part_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            progress.update(task, advance=len(chunk))

            # Rename .part file to final destination
            part_path.rename(dest_path)
            
            # Make the binary executable (chmod +x)
            if sys_name != "Windows":
                current_stat = dest_path.stat().st_mode
                dest_path.chmod(current_stat | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            print(f"[bold green][OK] Successfully cached {bin_name}[/bold green]")

        except Exception as e:
            if part_path.exists():
                part_path.unlink()
            print(f"[bold red][ERROR] Failed to download {bin_name}: {e}[/bold red]")
            raise typer.Exit(1)

    # After all downloads successfully complete, update the cached version file
    version_file = BINARY_DIR / ".version"
    try:
        version_file.write_text(version)
    except Exception:
        pass
