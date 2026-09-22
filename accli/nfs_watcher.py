import os
import sys
import time
import ctypes
import subprocess
from pathlib import Path
from datetime import datetime

# Windows Shell Notification constants
SHCNE_UPDATEDIR = 0x00001000
SHCNF_PATHW = 0x0005

# Win32 Synchronization constants
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002

kernel32 = ctypes.windll.kernel32


def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [Watcher] {msg}"
    print(line, flush=True)
    try:
        log_file = Path.home() / ".accli" / "nfs_watcher.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_sync_names(mount_point: str):
    """Returns unique Named Mutex and Event names based on mount drive."""
    drive = Path(mount_point).drive.upper().rstrip(":")
    tag = drive if drive else "DEFAULT"
    return f"Local\\accli_watcher_{tag}_mutex", f"Local\\accli_watcher_{tag}_stop"


def get_open_explorer_paths(script_path: Path, mount_point: str) -> list[str]:
    """Queries open Windows Explorer windows to get paths currently viewed under mount_point."""
    if not script_path.is_file():
        return []
    try:
        res = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(script_path), mount_point, "ListOpenPaths"
            ],
            capture_output=True,
            text=True,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
        if res.returncode == 0 and res.stdout:
            return [p.strip() for p in res.stdout.splitlines() if p.strip()]
    except Exception as e:
        log(f"Error querying open explorer paths: {e}")
    return []


def notify_explorer(script_path: Path, mount_point: str, path_str: str):
    """Notifies Windows Shell and refreshes Explorer windows for path_str."""
    log(f"Broadcasting SHChangeNotify on '{path_str}'...")
    try:
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_UPDATEDIR,
            SHCNF_PATHW,
            path_str,
            None
        )
    except Exception as e:
        log(f"SHChangeNotify error: {e}")

    if script_path.is_file():
        try:
            subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(script_path), mount_point, "Refresh", path_str
                ],
                capture_output=True,
                text=True,
                creationflags=0x08000000  # CREATE_NO_WINDOW
            )
        except Exception as e:
            log(f"COM refresh error: {e}")


def scan_directory_state(dir_path: Path):
    """
    Fast snapshot of a single directory using os.scandir().
    Avoids recursive network traversal. Returns dict of entry_name -> (is_dir, size, mtime_ns).
    """
    state = {}
    try:
        if not dir_path.exists():
            return None
        with os.scandir(dir_path) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                    is_dir = entry.is_dir(follow_symlinks=False)
                    state[entry.name] = (is_dir, st.st_size if not is_dir else -1, st.st_mtime_ns)
                except (OSError, PermissionError):
                    continue
        return state
    except Exception:
        return None


def describe_changes(old_state, new_state):
    old_keys = set(old_state.keys())
    new_keys = set(new_state.keys())
    added = new_keys - old_keys
    removed = old_keys - new_keys
    modified = {k for k in (old_keys & new_keys) if old_state[k] != new_state[k]}
    
    parts = []
    if added:
        parts.append(f"Added: {list(added)[:5]}")
    if removed:
        parts.append(f"Removed: {list(removed)[:5]}")
    if modified:
        parts.append(f"Modified: {list(modified)[:5]}")
    return "; ".join(parts)


def run_watcher(mount_point: str, interval: float):
    """
    Optimized NFS watcher:
    - Waits on a Named Event for instant, 0-CPU sleeping and clean shutdown.
    - Inspects only directories currently viewed in Windows Explorer (View-Scoped).
    - Uses shallow os.scandir() to avoid recursive network tree walking.
    """
    drive_root = mount_point.rstrip("\\/") + "\\"
    script_path = Path(__file__).parent / "refresh_explorer.ps1"
    interval_ms = max(500, int(interval * 1000))

    mutex_name, stop_event_name = get_sync_names(mount_point)

    # 1. Single-instance protection via Named Mutex
    mutex = kernel32.CreateMutexW(None, True, mutex_name)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        log(f"Another watcher is already active for {drive_root} ({mutex_name}). Exiting.")
        sys.exit(0)

    # 2. Named Event for clean cooperative shutdown
    stop_event = kernel32.CreateEventW(None, True, False, stop_event_name)

    # 3. Write user-scoped fallback PID file
    pid_file = Path.home() / ".accli" / "nfs_watcher.pid"
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    log(f"Starting view-scoped NFS watcher on '{drive_root}' (poll interval: {interval}s)...")

    cached_states = {}

    try:
        while True:
            # Sleep via WaitForSingleObject: 0% CPU, immediate wakeup on stop signal
            wait_res = kernel32.WaitForSingleObject(stop_event, interval_ms)
            if wait_res == WAIT_OBJECT_0:
                log("Stop event signaled. Gracefully exiting watcher.")
                break

            # If drive is unmounted or inaccessible, idle until available
            if not Path(drive_root).exists():
                continue

            # Query which folders under mount_point are currently open in Explorer
            open_dirs = get_open_explorer_paths(script_path, mount_point)

            # If no Explorer window is viewing the mount, zero network I/O needed
            if not open_dirs:
                if cached_states:
                    cached_states.clear()
                continue

            # Check each active directory
            for dir_str in open_dirs:
                dpath = Path(dir_str)
                current_state = scan_directory_state(dpath)
                if current_state is None:
                    continue

                last_state = cached_states.get(dir_str)
                if last_state is not None:
                    if current_state != last_state:
                        diff_summary = describe_changes(last_state, current_state)
                        log(f"Change detected in '{dir_str}' -> {diff_summary}")
                        notify_explorer(script_path, mount_point, dir_str)
                        cached_states[dir_str] = current_state
                else:
                    cached_states[dir_str] = current_state

    finally:
        # Cleanup
        try:
            if pid_file.is_file():
                pid_file.unlink()
        except Exception:
            pass
        if stop_event:
            kernel32.CloseHandle(stop_event)
        if mutex:
            kernel32.CloseHandle(mutex)
        log("Watcher terminated.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python nfs_watcher.py <mount_point> <interval_seconds>")
        sys.exit(1)
    mp = sys.argv[1]
    poll_int = float(sys.argv[2])
    run_watcher(mp, poll_int)

