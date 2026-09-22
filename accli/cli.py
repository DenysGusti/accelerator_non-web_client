import glob
import importlib.util
import os
import re
import time
import warnings
import hashlib
import json
import hf_xet
from contextlib import contextmanager
from pathlib import Path

import requests
import typer
from typing import List

from accli.AcceleratorTerminalCliProjectService import AcceleratorTerminalCliProjectService
from accli.CsvRegionalTimeseriesValidator import CsvRegionalTimeseriesValidator
from accli.token import save_token_details, get_token, get_server_url, set_project_slug, get_project_slug, get_db_path, exchange_refresh_token
from rich import print
from rich.progress import Progress, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn
from typing_extensions import Annotated

from ._version import VERSION
from . import mount_downloader

warnings.filterwarnings('ignore')

ACCLI_DEBUG = os.environ.get('ACCLI_DEBUG', False)

app = typer.Typer(
    add_completion=False,
    pretty_exceptions_show_locals=False,
    no_args_is_help=True
)


def get_size(path):
    size = 0

    for file in glob.iglob(f"{path}/**/*.*", recursive=True):
        size += os.path.getsize(file)

    return size


@contextmanager
def pushd(new_dir):
    """Temporarily change the working directory."""
    prev_dir = os.getcwd()
    os.chdir(new_dir)
    try:
        yield
    finally:
        os.chdir(prev_dir)


@app.command()
def about():
    print(
        "[bold cyan]This is a terminal client for Accelerator hosted on https://accelerator.iiasa.ac.at . [/bold cyan]\n")
    print(
        "[bold cyan]Please file feature requests and suggestions at https://github.com/iiasa/accli/issues .[/bold cyan]\n")
    print("[bold cyan]License: The MIT License (MIT)[/bold cyan]\n")
    print(f"[bold cyan]Version: {VERSION}[/bold cyan]\n")


@app.command()
def login(
        server: Annotated[
            str, typer.Option(..., '-s', help="Accelerator server url.")] = "https://accelerator.iiasa.ac.at",
        webcli: Annotated[str, typer.Option(..., '-c',
                                            help="Accelerator web client for authorization.")] = "https://accelerator.iiasa.ac.at"
):
    print(
        f"[bold cyan]Welcome to Accelerator Terminal Client.[/bold cyan]\n"
        f"[bold cyan]Powered by IIASA[/bold cyan]\n"
    )

    print(f"[italic]Get authorization code on following web url: [cyan]{webcli}/acli-auth-code[/cyan][/italic] \n")

    device_authorization_code = typer.prompt("Enter the authorization code?")

    token_response = requests.post(
        f"{server}/api/v1/oauth/device/token/",
        json={"device_authorization_code": device_authorization_code},
        verify=(not ACCLI_DEBUG)
    )

    print("")

    if token_response.status_code == 400:
        print(f"[bold red]ERROR: {token_response.json().get('detail')}[/bold red]")
        raise typer.Exit(1)

    save_token_details(token_response.json(), server, webcli)

    print("[bold green]Successfully logged in.[/bold green]:rocket: :rocket:")


# Upload functionality is now integrated into the unified 'copy' command.


@app.command()
def validate(
        project_slug: Annotated[str, typer.Argument(help="Unique Accelerator project slug.")],
        template_slug: Annotated[str, typer.Argument(help="Unique project template slug")],
        filepath: Annotated[str, typer.Argument(help="Path of the file to validate")],
        server: Annotated[
            str, typer.Option(..., '-s', help="Accelerator server url.")] = "https://accelerator.iiasa.ac.at",
):
    term_cli_project_service = AcceleratorTerminalCliProjectService(
        user_token="",
        server_url=server,
        verify_cert=(not ACCLI_DEBUG)
    )

    validate = CsvRegionalTimeseriesValidator(
        project_slug=project_slug,
        dataset_template_slug=template_slug,
        input_filepath=filepath,
        project_service=term_cli_project_service,
    )

    validate()


@app.command()
def dispatch(
        project_slug: Annotated[str, typer.Argument(help="Unique Accelerator project slug.")],
        root_task_variable: Annotated[str, typer.Argument(help="Root task variable in workflow_file.")],
        workflow_filename: Annotated[str, typer.Option(..., '-f', help="Python workflow filepath.")] = "wkube.py"
):
    set_project_slug(project_slug)
    server_url = get_server_url()
    # Exchange the refresh token for a short-lived access token
    _, access_token, _ = exchange_refresh_token(project_slug)

    term_cli_project_service = AcceleratorTerminalCliProjectService(
        user_token=access_token,
        server_url=server_url,
        verify_cert=(not ACCLI_DEBUG)
    )

    #  Resolve the workflow file path properly
    if os.path.isabs(workflow_filename):
        workflow_filepath = workflow_filename
    else:
        workflow_filepath = os.path.abspath(os.path.join(os.getcwd(), workflow_filename))

    if not os.path.isfile(workflow_filepath):
        raise FileNotFoundError(f"Workflow file not found: {workflow_filepath}")

    workflow_dir = os.path.dirname(workflow_filepath)

    #  Temporarily switch to the workflow file's directory
    with pushd(workflow_dir):
        #  Import the module dynamically
        spec = importlib.util.spec_from_file_location("workflow", workflow_filepath)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        job_to_dispatch = getattr(module, root_task_variable, None)

        if not job_to_dispatch:
            raise ValueError(f"No root task variable found with name '{root_task_variable}' in {workflow_filepath}")

        print(job_to_dispatch.description)

        root_job_id = term_cli_project_service.dispatch(
            project_slug,
            job_to_dispatch.description
        )

    print(f"Dispatched root job #ID: {root_job_id}")


def compute_sha256(filepath: str) -> str:
    """Helper to compute the SHA-256 hash of a file locally."""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()




@app.command()
def copy(
        source: Annotated[str, typer.Argument(help="Source path (local path or remote acc://...)")],
        destination: Annotated[str, typer.Argument(help="Destination path (local path or remote acc://...)")],
        token_pass: Annotated[str, typer.Option(..., "-t", help="Optional authorization token pass.")] = "",
):
    """
    Copy files between local filesystems and Accelerator project spaces using hf-xet.
    At least one of the paths must be a remote path starting with 'acc://'.
    """
    is_src_remote = source.startswith("acc://")
    is_dest_remote = destination.startswith("acc://")

    if not is_src_remote and not is_dest_remote:
        print("[bold red]ERROR: Either source or destination must start with acc://[/bold red]")
        raise typer.Exit(1)
    if is_src_remote and is_dest_remote:
        print("[bold red]ERROR: Remote-to-remote copy is not supported. One path must be local.[/bold red]")
        raise typer.Exit(1)

    # Resolve project_slug from the remote path
    remote_path_str = source if is_src_remote else destination
    parsed = remote_path_str[len("acc://"):]
    if "/" in parsed:
        project_slug, remote_subpath = parsed.split("/", 1)
    else:
        project_slug = parsed
        remote_subpath = ""

    server_url = get_server_url()
    cas_token, access_token, expires_at = exchange_refresh_token(project_slug)

    term_cli_project_service = AcceleratorTerminalCliProjectService(
        user_token=access_token,
        server_url=server_url,
        verify_cert=(not ACCLI_DEBUG),
    )

    if is_src_remote:
        # Download flow: acc://[project_slug]/[remote_prefix] -> local destination
        source_parsed = source[len("acc://"):]
        if "/" in source_parsed:
            _, remote_prefix = source_parsed.split("/", 1)
        else:
            remote_prefix = ""

        cas_endpoint = f"{server_url.rstrip('/')}/api/xet-cas"

        print(f"[cyan]Enumerating remote files for prefix [white]{source_parsed}[/white]...[/cyan]")
        filenames: List[str] = term_cli_project_service.enumerate_files_by_prefix(source_parsed, token_pass=token_pass)

        if not filenames:
            print("[bold yellow]No files found matching the remote prefix.[/bold yellow]")
            raise typer.Exit(0)

        dest_path = Path(destination).expanduser().resolve()
        is_dest_dir = (
            dest_path.is_dir()
            or destination.endswith("/")
            or destination.endswith(os.sep)
            or len(filenames) > 1
        )

        if is_dest_dir:
            dest_path.mkdir(parents=True, exist_ok=True)
        else:
            dest_path.parent.mkdir(parents=True, exist_ok=True)

        files_to_download = []
        for filename in filenames:
            # Filename returned usually starts with project_slug/
            if filename.startswith(f"{project_slug}/"):
                rel_filename = filename[len(project_slug):]
            elif filename.startswith(project_slug):
                rel_filename = filename[len(project_slug):]
            else:
                rel_filename = filename

            if not rel_filename.startswith("/"):
                rel_filename = "/" + rel_filename

            print(f"[dim]Resolving metadata for {filename}...[/dim]")
            stat = term_cli_project_service.get_file_stat(project_slug, rel_filename)
            if not stat:
                print(f"[yellow]Warning: Could not get metadata for {filename}, skipping.[/yellow]")
                continue

            merkle_hash = stat.get("merkle_hash")
            file_size = stat.get("size")
            if not merkle_hash or file_size is None:
                print(f"[yellow]Warning: Missing merkle hash or size for {filename}, skipping.[/yellow]")
                continue

            if is_dest_dir:
                # Determine local subpath under destination
                if remote_prefix:
                    prefix_to_strip = f"{project_slug}/{remote_prefix}"
                    if filename.startswith(prefix_to_strip):
                        rel_subpath = filename[len(prefix_to_strip):].lstrip("/")
                    else:
                        rel_subpath = filename[len(project_slug):].lstrip("/")
                else:
                    rel_subpath = filename[len(project_slug):].lstrip("/")

                local_dest = dest_path / rel_subpath
            else:
                local_dest = dest_path

            local_dest.parent.mkdir(parents=True, exist_ok=True)

            files_to_download.append(
                (str(local_dest.resolve()), merkle_hash, file_size)
            )

        if not files_to_download:
            print("[bold red]ERROR: No valid files identified for download.[/bold red]")
            raise typer.Exit(1)

        MAX_FILES_PER_BATCH = 100
        MAX_BYTES_PER_BATCH = 10 * 1024 * 1024 * 1024 # 10 GB
        
        batches = []
        current_batch = []
        current_batch_size = 0
        
        for item in files_to_download:
            size = item[2]
            if size >= MAX_BYTES_PER_BATCH and not current_batch:
                batches.append([item])
                continue
                
            if len(current_batch) >= MAX_FILES_PER_BATCH or current_batch_size + size > MAX_BYTES_PER_BATCH:
                batches.append(current_batch)
                current_batch = []
                current_batch_size = 0
                
            current_batch.append(item)
            current_batch_size += size
            
        if current_batch:
            batches.append(current_batch)

        print(f"[bold cyan]Downloading {len(files_to_download)} files in {len(batches)} batches using hf-xet...[/bold cyan]")
        
        refresh_url = f"{server_url.rstrip('/')}/api/v1/oauth/device/cas-token/"
        refresh_headers = {"Authorization": f"Bearer {cas_token}"}
        
        import sys
        columns = [
            TextColumn("[progress.description]{task.description}"),
            TaskProgressColumn(),
            TimeElapsedColumn(),
        ]
        if sys.stdout.isatty():
            columns.insert(1, BarColumn())
            
        with Progress(*columns) as progress:
            total_size = sum(item[2] for item in files_to_download)
            overall_task = progress.add_task("[green]Total Download Progress...", total=total_size)
            
            completed_in_previous_batches = 0
            
            session = hf_xet.XetSession()
            for batch_index, batch_items in enumerate(batches, 1):
                progress.print(f"[cyan]Processing batch {batch_index}/{len(batches)}...[/cyan]")
                
                # capture current completed_in_previous_batches in a local scope to avoid late binding issues
                file_tasks = {}
                
                def make_progress_handler(base_completed):
                    def on_progress(group_report, item_reports):
                        progress.update(overall_task, completed=base_completed + group_report.total_bytes_completed)
                        for item_id, item in item_reports.items():
                            if item_id not in file_tasks:
                                file_tasks[item_id] = progress.add_task(f"[blue]{item.item_name}", total=item.total_bytes)
                            
                            progress.update(file_tasks[item_id], completed=item.bytes_completed)
                            
                            # Hide completed tasks to keep the UI clean when downloading thousands of files
                            if item.bytes_completed >= item.total_bytes:
                                progress.update(file_tasks[item_id], visible=False)
                    return on_progress
                    
                try:
                    with session.new_file_download_group(
                        endpoint=cas_endpoint,
                        token=cas_token,
                        token_expiry_unix_secs=expires_at,
                        token_refresh_url=refresh_url,
                        token_refresh_headers=refresh_headers,
                        progress_callback=make_progress_handler(completed_in_previous_batches),
                        progress_interval_ms=500,
                    ) as group:
                        for dest_path, hash_val, size_val in batch_items:
                            info = hf_xet.XetFileInfo(hash_val, size_val)
                            group.start_download_file(info, dest_path)
                            
                    batch_size = sum(item[2] for item in batch_items)
                    completed_in_previous_batches += batch_size
                    progress.update(overall_task, completed=completed_in_previous_batches)
                    progress.print(f"[bold green][OK] Batch {batch_index} completed successfully![/bold green]")
                except Exception as e:
                    progress.print(f"[bold red]ERROR: Download failed for batch {batch_index}: {e}[/bold red]")
                    raise typer.Exit(1)

        print("[bold green][OK] Download completed successfully![/bold green]")

    else:
        # Upload flow: local source -> acc://[project_slug]/[remote_path]
        dest_parsed = destination[len("acc://"):]
        if "/" in dest_parsed:
            project_slug, remote_path = dest_parsed.split("/", 1)
        else:
            project_slug = dest_parsed
            remote_path = ""

        cas_endpoint = f"{server_url.rstrip('/')}/api/xet-cas"
        register_url = f"{server_url.rstrip('/')}/api/xet-cas/v1/cas/bulk-register"

        local_src_path = Path(source).expanduser().resolve()
        if not local_src_path.exists():
            print(f"[bold red]ERROR: Local source path does not exist: {source}[/bold red]")
            raise typer.Exit(1)

        # Collect local files and map to remote filenames
        local_paths = []
        remote_filenames = []

        if local_src_path.is_dir():
            # Recursively find all files
            for local_file in local_src_path.rglob("*"):
                if local_file.is_file():
                    rel_subpath = local_file.relative_to(local_src_path)
                    # Remote filename should be relative under project_slug
                    if remote_path:
                        remote_name = f"{remote_path.rstrip('/')}/{rel_subpath}"
                    else:
                        remote_name = str(rel_subpath)
                    
                    if os.name == 'nt':
                        remote_name = remote_name.replace('\\', '/')
                        
                    local_paths.append(local_file)
                    remote_filenames.append(remote_name)
        else:
            # Single file
            if remote_path.endswith("/") or not remote_path:
                remote_name = f"{remote_path.rstrip('/')}/{local_src_path.name}".lstrip("/")
            else:
                remote_name = remote_path
                
            local_paths.append(local_src_path)
            remote_filenames.append(remote_name)

        if not local_paths:
            print("[bold yellow]No files to upload.[/bold yellow]")
            raise typer.Exit(0)

        MAX_FILES_PER_BATCH = 100
        MAX_BYTES_PER_BATCH = 10 * 1024 * 1024 * 1024 # 10 GB
        
        batches = []
        current_batch_paths = []
        current_batch_remote = []
        current_batch_size = 0
        
        for local_p, remote_n in zip(local_paths, remote_filenames):
            size = local_p.stat().st_size
            if size >= MAX_BYTES_PER_BATCH and not current_batch_paths:
                batches.append(([local_p], [remote_n]))
                continue
                
            if len(current_batch_paths) >= MAX_FILES_PER_BATCH or current_batch_size + size > MAX_BYTES_PER_BATCH:
                batches.append((current_batch_paths, current_batch_remote))
                current_batch_paths = []
                current_batch_remote = []
                current_batch_size = 0
                
            current_batch_paths.append(local_p)
            current_batch_remote.append(remote_n)
            current_batch_size += size
            
        if current_batch_paths:
            batches.append((current_batch_paths, current_batch_remote))

        print(f"[bold cyan]Uploading {len(local_paths)} files in {len(batches)} batches using hf-xet...[/bold cyan]")
        
        refresh_url = f"{server_url.rstrip('/')}/api/v1/oauth/device/cas-token/"
        refresh_headers = {"Authorization": f"Bearer {cas_token}"}
        
        import sys
        columns = [
            TextColumn("[progress.description]{task.description}"),
            TaskProgressColumn(),
            TimeElapsedColumn(),
        ]
        if sys.stdout.isatty():
            columns.insert(1, BarColumn())
            
        with Progress(*columns) as progress:
            total_size = sum(p.stat().st_size for p in local_paths)
            overall_task = progress.add_task("[green]Total Upload Progress...", total=total_size)
            
            completed_in_previous_batches = 0
            
            session = hf_xet.XetSession()
            for batch_index, (b_paths, b_remotes) in enumerate(batches, 1):
                progress.print(f"[cyan]Processing batch {batch_index}/{len(batches)}...[/cyan]")
                
                file_tasks = {}
                
                def make_progress_handler(base_completed):
                    def on_progress(group_report, item_reports):
                        progress.update(overall_task, completed=base_completed + group_report.total_bytes_completed)
                        for item_id, item in item_reports.items():
                            if item_id not in file_tasks:
                                file_tasks[item_id] = progress.add_task(f"[blue]{item.item_name}", total=item.total_bytes)
                            
                            progress.update(file_tasks[item_id], completed=item.bytes_completed)
                            
                            # Hide completed tasks to keep the UI clean
                            if item.bytes_completed >= item.total_bytes:
                                progress.update(file_tasks[item_id], visible=False)
                    return on_progress
                    
                try:
                    with session.new_upload_commit(
                        endpoint=cas_endpoint,
                        token=cas_token,
                        token_expiry_unix_secs=expires_at,
                        token_refresh_url=refresh_url,
                        token_refresh_headers=refresh_headers,
                        progress_callback=make_progress_handler(completed_in_previous_batches),
                        progress_interval_ms=500,
                    ) as commit:
                        handles = []
                        for p in b_paths:
                            sha = compute_sha256(str(p))
                            h = commit.start_upload_file(str(p), sha256=sha)
                            handles.append((p, sha, h))
                        
                    upload_results = [(p, sha, h.result()) for p, sha, h in handles]
                except Exception as e:
                    progress.print(f"[bold red]ERROR: Upload failed for batch {batch_index}: {e}[/bold red]")
                    raise typer.Exit(1)

                progress.print("[cyan]Computing SHA-256 hashes and registering metadata for batch...[/cyan]")
                registration_items = []
                for local_path, remote_filename, (p, sha256_hash, upload_info) in zip(b_paths, b_remotes, upload_results):
                    registration_items.append({
                        "filename": f"{project_slug}/{remote_filename}",
                        "merkle_hash": upload_info.xet_info.hash,
                        "sha256": sha256_hash,
                        "file_size": upload_info.xet_info.file_size,
                        "content_type": "application/octet-stream"
                    })

                headers = {
                    "Content-Type": "application/json",
                    "X-Project-Slug": project_slug,
                    "Authorization": f"Bearer {cas_token}"
                }

                try:
                    response = requests.post(
                        register_url,
                        json={"items": registration_items},
                        headers=headers,
                        verify=(not ACCLI_DEBUG)
                    )
                    response.raise_for_status()
                    
                    batch_size = sum(p.stat().st_size for p in b_paths)
                    completed_in_previous_batches += batch_size
                    progress.update(overall_task, completed=completed_in_previous_batches)
                    
                    progress.print(f"[bold green][OK] Batch {batch_index} registered successfully![/bold green]")
                except requests.exceptions.HTTPError as e:
                    detail = None
                    try:
                        detail = e.response.json().get("detail")
                    except Exception:
                        pass
                    if detail:
                        progress.print(f"[bold red]ERROR: Bulk metadata registration failed for batch {batch_index}: {detail}[/bold red]")
                    else:
                        progress.print(f"[bold red]ERROR: Bulk metadata registration failed for batch {batch_index}: {e}[/bold red]")
                    raise typer.Exit(1)
                except Exception as e:
                    progress.print(f"[bold red]ERROR: Bulk metadata registration failed for batch {batch_index}: {e}[/bold red]")
                    raise typer.Exit(1)

        print("[bold green][OK] Upload completed successfully![/bold green]")


# --- Mount commands group ---
mount_app = typer.Typer(
    help="Manage mounting Accelerator project spaces locally using hf-mount.",
    no_args_is_help=True
)
app.add_typer(mount_app, name="mount")


def find_available_windows_drive(preferred: str = "W") -> str:
    """Finds an available Windows drive letter, starting with preferred, then checking others."""
    import os
    preferred = preferred.upper()
    if not os.path.exists(f"{preferred}:\\"):
        return f"{preferred}:"
        
    # Search order: subsequent letters first, then previous descending to D
    search_order = ["X", "Y", "Z"] + [chr(x) for x in range(ord("V"), ord("D") - 1, -1)]
    for letter in search_order:
        if not os.path.exists(f"{letter}:\\"):
            return f"{letter}:"
            
    raise RuntimeError("No available Windows drive letters found.")


def start_nfs_watcher(mount_point: str, interval: int):
    """Starts a lightweight background watcher in the user session that notifies Explorer when remote changes occur."""
    import sys
    import subprocess
    from pathlib import Path
    
    watcher_script = Path(__file__).parent / "nfs_watcher.py"
    if not watcher_script.is_file():
        return
    
    # Locate Python interpreter (prefer pythonw to run completely windowless if available)
    py_exec = sys.executable
    parent_dir = Path(py_exec).parent
    pythonw = parent_dir / "pythonw.exe"
    if pythonw.is_file():
        executable = str(pythonw)
    else:
        executable = py_exec
        
    try:
        # DETACHED_PROCESS (0x00000008) | CREATE_NEW_PROCESS_GROUP (0x00000200) | CREATE_NO_WINDOW (0x08000000)
        creationflags = 0x08000008 | 0x00000200
        proc = subprocess.Popen(
            [executable, str(watcher_script), str(mount_point), str(interval)],
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True
        )
        pid_file = Path.home() / ".accli" / "nfs_watcher.pid"
        try:
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(proc.pid), encoding="utf-8")
        except Exception:
            pass
    except Exception:
        pass


def stop_nfs_watcher(mount_point: str):
    """Signals and stops any active background NFS watcher process."""
    import os
    import subprocess
    from pathlib import Path
    import ctypes
    kernel32 = ctypes.windll.kernel32
    
    # 1. Graceful cooperative shutdown via Windows Named Event
    try:
        EVENT_MODIFY_STATE = 0x0002
        drive = Path(mount_point).drive.upper().rstrip(":")
        evt_name = f"Local\\accli_watcher_{drive}_stop" if drive else "Local\\accli_watcher_stop"
        h_evt = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, evt_name)
        if h_evt:
            kernel32.SetEvent(h_evt)
            kernel32.CloseHandle(h_evt)
            time.sleep(0.1)
    except Exception:
        pass

    # 2. Clean up user-scoped PID file
    pid_file = Path.home() / ".accli" / "nfs_watcher.pid"
    if pid_file.is_file():
        try:
            pid_str = pid_file.read_text(encoding="utf-8").strip()
            if pid_str and pid_str != str(os.getpid()):
                subprocess.run(["taskkill", "/F", "/PID", pid_str], capture_output=True)
        except Exception:
            pass
        try:
            pid_file.unlink()
        except Exception:
            pass


def enable_windows_nfs_features():
    """Automatically check and enable Windows Client for NFS features, registry key, and Task Scheduler gateway if needed."""
    import subprocess
    import tempfile
    import os
    
    print("[cyan]Auditing Windows Client for NFS system configuration...[/cyan]")
    
    # 1. Non-elevated audit queries for Features, Registry, and Task Scheduler Gateway
    check_script = (
        "$nfsEnabled = Test-Path 'C:\\Windows\\System32\\mount.exe'\n"
        "\n"
        "$regEnabled = $false\n"
        "$regPath = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System'\n"
        "if (Test-Path $regPath) {\n"
        "    $val = Get-ItemProperty -Path $regPath -Name 'EnableLinkedConnections' -ErrorAction SilentlyContinue\n"
        "    if ($val -and $val.EnableLinkedConnections -eq 1) { $regEnabled = $true }\n"
        "}\n"
        "\n"
        "$anonEnabled = $false\n"
        "$anonPath = 'HKLM:\\SOFTWARE\\Microsoft\\ClientForNFS\\CurrentVersion\\Default'\n"
        "if (Test-Path $anonPath) {\n"
        "    $valUid = Get-ItemProperty -Path $anonPath -Name 'AnonymousUid' -ErrorAction SilentlyContinue\n"
        "    $valGid = Get-ItemProperty -Path $anonPath -Name 'AnonymousGid' -ErrorAction SilentlyContinue\n"
        "    if ($null -ne $valUid -and $valUid.AnonymousUid -eq 0 -and $null -ne $valGid -and $valGid.AnonymousGid -eq 0) { $anonEnabled = $true }\n"
        "}\n"
        "\n"
        "$cacheEnabled = $false\n"
        "$cachePath = 'HKLM:\\SOFTWARE\\Microsoft\\ClientForNFS\\CurrentVersion\\Users\\Default\\Cache'\n"
        "if (Test-Path $cachePath) {\n"
        "    $valDelta = Get-ItemProperty -Path $cachePath -Name 'AttributeTimeDelta' -ErrorAction SilentlyContinue\n"
        "    if ($null -ne $valDelta -and $valDelta.AttributeTimeDelta -ge 1) { $cacheEnabled = $true }\n"
        "}\n"
        "\n"
        "$taskEnabled = $false\n"
        "if (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue) {\n"
        "    $t = Get-ScheduledTask -TaskName 'accli-mount-nfs' -ErrorAction SilentlyContinue\n"
        "    if ($t) { $taskEnabled = $true }\n"
        "} else {\n"
        "    $query = schtasks /query /tn 'accli-mount-nfs' 2> $null\n"
        "    if ($LASTEXITCODE -eq 0) { $taskEnabled = $true }\n"
        "}\n"
        "\n"
        "Write-Output \"NFS:$nfsEnabled;REG:$regEnabled;ANON:$anonEnabled;CACHE:$cacheEnabled;TASK:$taskEnabled\""
    )
    
    check_cmd = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
        check_script
    ]
    
    nfs_ok = False
    reg_ok = False
    anon_ok = False
    cache_ok = False
    task_ok = False
    
    try:
        res = subprocess.run(check_cmd, capture_output=True, text=True, check=True)
        output = res.stdout.strip()
        for part in output.split(';'):
            if part.startswith("NFS:"):
                nfs_ok = (part.split(':')[1].lower() == 'true')
            elif part.startswith("REG:"):
                reg_ok = (part.split(':')[1].lower() == 'true')
            elif part.startswith("ANON:"):
                anon_ok = (part.split(':')[1].lower() == 'true')
            elif part.startswith("CACHE:"):
                cache_ok = (part.split(':')[1].lower() == 'true')
            elif part.startswith("TASK:"):
                task_ok = (part.split(':')[1].lower() == 'true')
    except Exception as e:
        print(f"[yellow]Warning: Could not audit current Windows NFS settings: {e}[/yellow]")
    
    # Require general registry settings, Anonymous mappings, and Cache policies to be correct
    reg_ok = reg_ok and anon_ok and cache_ok
    
    if nfs_ok and reg_ok and task_ok:
        print("[bold green][OK] Windows NFS client features, registry policies, and Task Scheduler gateway are already configured.[/bold green]")
        return
        
    print("[yellow]Administrative setup is required to configure the Windows NFS mount gateway.[/yellow]")
    print("[italic]A UAC administrator elevation prompt will appear shortly. Please approve it...[/italic]")
    
    # 2. Locate and load the static setup-nfs.ps1 script (Single Source of Truth)
    import sys
    from pathlib import Path
    
    script_dir = Path(__file__).parent
    if hasattr(sys, "_MEIPASS"):
        setup_script_path = Path(sys._MEIPASS) / "setup-nfs.ps1"
    else:
        setup_script_path = script_dir / "setup-nfs.ps1"

    if not setup_script_path.is_file():
        # Fallback to dev workspace relative path just in case
        dev_path = (script_dir / ".." / "accli" / "setup-nfs.ps1").resolve()
        if dev_path.is_file():
            setup_script_path = dev_path
        else:
            print("[bold red]ERROR: Windows NFS Setup script 'setup-nfs.ps1' not found inside package.[/bold red]")
            raise typer.Exit(1)

    try:
        setup_script_content = setup_script_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[bold red]ERROR: Failed to read setup-nfs.ps1: {e}[/bold red]")
        raise typer.Exit(1)
        
    temp_dir = tempfile.gettempdir()
    temp_file_path = os.path.join(temp_dir, "accli_nfs_setup.ps1")
    
    try:
        with open(temp_file_path, "w", encoding="utf-8") as f:
            f.write(setup_script_content)
            
        # Build arguments for Start-Process to pass state as parameters
        ps_params = []
        if nfs_ok:
            ps_params.append("-NfsAlreadyEnabled")
        if reg_ok:
            ps_params.append("-RegAlreadyEnabled")
        
        params_str = " ".join(ps_params)
        elevate_cmd = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
            f"Start-Process powershell.exe -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"{temp_file_path}\" {params_str}' -Verb RunAs -Wait"
        ]
        
        result = subprocess.run(elevate_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("[bold red][ERROR] Administrative configuration failed or UAC prompt was denied.[/bold red]")
            if result.stderr:
                print(f"[red]{result.stderr.strip()}[/red]")
            raise typer.Exit(1)
            
        print("[bold green][OK] Elevated administrative setup completed successfully![/bold green]")
        
        # Reboot advisory for registry keys or NFS features
        if not reg_ok or not nfs_ok:
            print("")
            print("[bold yellow][WARNING] IMPORTANT: A Windows system restart is required for the new NFS Client features and registry policies to take effect.[/bold yellow]")
            print("[bold yellow]Please restart your computer, then run 'accli mount start' again to mount your drive passwordlessly![/bold yellow]")
            print("")
            raise typer.Exit(0)
            
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise e
        print(f"[bold red]ERROR: Administrative configuration could not be executed: {e}[/bold red]")
        print("[yellow]Please run the following commands manually in an elevated/Administrator PowerShell window:[/yellow]")
        print("  Enable-WindowsOptionalFeature -Online -FeatureName ServicesForNFS-ClientOnly -All")
        print("  Enable-WindowsOptionalFeature -Online -FeatureName ClientForNFS-Infrastructure -All")
        print("  reg add HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System /v EnableLinkedConnections /t REG_DWORD /d 1 /f")
        raise typer.Exit(1)
    finally:
        if os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception:
                pass


@mount_app.command("start")
def mount_start(
    project_slug: Annotated[str, typer.Argument(help="Unique Accelerator project slug, bucket, or repository ID.")],
    mount_point: Annotated[Path, typer.Argument(help="Local directory path where the filesystem will be mounted. (Defaults: W: or first available drive on Windows, ~/accelerator/mnt/<project_slug> on Unix/Linux).")] = None,
    mode: Annotated[str, typer.Option("--mode", "-m", help="Mounting mode: 'bucket' (read-write, default) or 'repo' (read-only).")] = "bucket",
    fuse: Annotated[bool, typer.Option("--fuse", "-f", help="Use FUSE backend instead of the default NFS backend.")] = False,
    overlay: Annotated[bool, typer.Option("--overlay", "-o", help="Enable overlay mode (local writes only, remote read-only).")] = False,
    read_only: Annotated[bool, typer.Option("--read-only", "-r", help="Force read-only mount.")] = False,
    binary_version: Annotated[str, typer.Option("--binary-version", "-b", help="Specific hf-mount release version to download.")] = mount_downloader.DEFAULT_VERSION,
):
    """
    Start a mount as a background daemon.
    """
    from . import mount_downloader
    import platform
    import re
    import json
    
    sys_name = platform.system()
    
    # Get credentials
    try:
        token = get_token()
        server_url = get_server_url()
    except Exception as e:
        print(f"[bold red]ERROR: Could not retrieve login details. Please run 'accli login' first. Details: {e}[/bold red]")
        raise typer.Exit(1)

    # Validate mode
    if mode == "repo":
        print("[bold red]ERROR: 'repo' mode is not implemented yet. Please use 'bucket' mode.[/bold red]")
        raise typer.Exit(1)
    elif mode != "bucket":
        print("[bold red]ERROR: Mode must be either 'bucket' or 'repo'.[/bold red]")
        raise typer.Exit(1)

    # Ensure binaries are downloaded and cached
    try:
        mount_downloader.ensure_binaries(version=binary_version, use_fuse=fuse)
    except Exception as e:
        print(f"[bold red]ERROR: Binary download/preparation failed: {e}[/bold red]")
        raise typer.Exit(1)

    # Standardize mount_point and handle defaults / auto-selection
    if sys_name == "Windows":
        # Check if the daemon is already running to prevent double mount
        import subprocess
        try:
            task_check = subprocess.run(["tasklist", "/FI", "IMAGENAME eq hf-mount-nfs.exe"], capture_output=True, text=True)
            if "hf-mount-nfs.exe" in task_check.stdout:
                config_file = Path("C:/ProgramData/accli/mount_config.json")
                active_msg = ""
                if config_file.is_file():
                    try:
                        import json
                        cfg = json.loads(config_file.read_text(encoding="utf-8"))
                        active_msg = f" for project '{cfg.get('project_slug')}' at '{cfg.get('mount_point')}'"
                    except Exception:
                        pass
                print(f"[bold red]ERROR: A mount daemon is already running{active_msg}.[/bold red]")
                print("[yellow]Please run 'accli mount stop' to unmount before starting a new mount.[/yellow]")
                raise typer.Exit(1)
        except Exception as e:
            if isinstance(e, typer.Exit):
                raise e

        if mount_point is None:
            # Auto-detect available drive letter starting with W
            try:
                drive = find_available_windows_drive("W")
                mount_point_abs = Path(drive)
                print(f"[cyan]No mount point specified. Selected available drive letter: [bold white]{drive}[/bold white][/cyan]")
            except Exception as e:
                print(f"[bold red]ERROR: {e}[/bold red]")
                raise typer.Exit(1)
        else:
            mount_point_str = str(mount_point)
            if re.match(r"^[a-zA-Z]:?\\?$", mount_point_str) or re.match(r"^[a-zA-Z]:/?$", mount_point_str):
                drive_letter = mount_point_str[0].upper()
                if os.path.exists(f"{drive_letter}:\\"):
                    print(f"[yellow]Warning: Drive '{drive_letter}:' is already in use.[/yellow]")
                    try:
                        fallback_drive = find_available_windows_drive("W")
                        print(f"[cyan]Falling back to first available drive: [bold white]{fallback_drive}[/bold white][/cyan]")
                        mount_point_abs = Path(fallback_drive)
                    except Exception as e:
                        print(f"[bold red]ERROR: Drive is in use and no alternative could be found: {e}[/bold red]")
                        raise typer.Exit(1)
                else:
                    mount_point_abs = Path(f"{drive_letter}:")
            else:
                mount_point_abs = mount_point.resolve()
    else:
        if mount_point is None:
            # Default mount point on Unix: ~/accelerator/mnt/<project_slug>
            mnt_dir = Path.home() / "accelerator" / "mnt" / project_slug
            mnt_dir.mkdir(parents=True, exist_ok=True)
            mount_point_abs = mnt_dir.resolve()
            print(f"[cyan]No mount point specified. Selected default directory: [bold white]{mount_point_abs}[/bold white][/cyan]")
        else:
            mount_point_abs = mount_point.resolve()

    # Construct environment
    env = os.environ.copy()
    env["ACCELERATOR_MOUNT"] = "1"
    env["ACC_ENDPOINT"] = server_url
    env["ACC_CAS_ENDPOINT"] = f"{server_url.rstrip('/')}/api/xet-cas"
    env["ACC_TOKEN"] = token

    if sys_name == "Windows":
        # Enable Windows NFS Client features if needed
        enable_windows_nfs_features()
        
        hf_mount_bin = mount_downloader.get_binary_path("hf-mount-nfs")
        if not hf_mount_bin.is_file():
            import shutil
            found_bin = shutil.which("hf-mount-nfs")
            if found_bin:
                hf_mount_bin = Path(found_bin)
            else:
                print("[bold red]ERROR: hf-mount-nfs binary not found in cache or system PATH.[/bold red]")
                raise typer.Exit(1)

        # Windows NFS Mount Setup
        db_path = os.path.normpath(get_db_path())
        args = [
            str(hf_mount_bin),
            "--token-file", db_path,
            "--hub-endpoint", server_url,
            mode,
            project_slug,
            str(mount_point_abs)
        ]
        if overlay:
            args.append("--overlay")
        if read_only:
            args.append("--read-only")

        print(f"[bold cyan]Starting Windows NFS mount for project '[white]{project_slug}[/white]' at '[white]{mount_point_abs}[/white]' in background...[/bold cyan]")
        
        # Prepare background logger
        log_file_path = Path.home() / ".accli" / "mount.log"
        # Ensure directory exists
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_file = open(log_file_path, "a", encoding="utf-8")
        except Exception:
            log_file = subprocess.DEVNULL

        import subprocess
        try:
            import ctypes
            is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            is_admin = False

        # Try to use Windows Task Scheduler to bypass UAC if registered
        has_task = False
        if not is_admin:
            try:
                task_check = subprocess.run(["schtasks", "/query", "/tn", "accli-mount-nfs"], capture_output=True, text=True)
                if task_check.returncode == 0:
                    has_task = True
            except Exception:
                pass

        if has_task:
            print("[cyan]Triggering elevated NFS mount via Task Scheduler (no UAC prompt)...[/cyan]")
            config_dir = Path("C:/ProgramData/accli")
            try:
                config_dir.mkdir(parents=True, exist_ok=True)
                
                # Copy binary to C:\ProgramData\accli to avoid profile execution policies for SYSTEM user
                target_bin = config_dir / "hf-mount-nfs.exe"
                try:
                    import shutil
                    import sys
                    if not target_bin.is_file() or target_bin.stat().st_size != hf_mount_bin.stat().st_size:
                        shutil.copy2(hf_mount_bin, target_bin)
                    
                    # Copy MSVC runtime DLLs to ProgramData so SYSTEM user can find them when running as a background task
                    base_prefix = Path(sys.base_exec_prefix)
                    for dll_name in ["vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"]:
                        dll_path = base_prefix / dll_name
                        if not dll_path.is_file():
                            found = shutil.which(dll_name)
                            if found:
                                dll_path = Path(found)
                        
                        target_dll = config_dir / dll_name
                        if dll_path.is_file():
                            if not target_dll.is_file() or target_dll.stat().st_size != dll_path.stat().st_size:
                                try:
                                    shutil.copy2(dll_path, target_dll)
                                except Exception:
                                    pass
                except Exception:
                    pass
                
                config_data = {
                    "token": token,
                    "server_url": server_url,
                    "mode": mode,
                    "project_slug": project_slug,
                    "mount_point": str(mount_point_abs),
                    "hf_mount_bin": str(target_bin),
                    "db_path": db_path,
                    "overlay": overlay,
                    "read_only": read_only,
                    "skip_auto_mount": True  # Force daemon only, we will map the drive letter in the user session!
                }
                config_file = config_dir / "mount_config.json"
                config_file.write_text(json.dumps(config_data, indent=2), encoding="utf-8")
                
                run_res = subprocess.run(["schtasks", "/run", "/tn", "accli-mount-nfs"], capture_output=True, text=True)
                if run_res.returncode == 0:
                    print("[cyan]Waiting for elevated NFS daemon to initialize...[/cyan]")
                    time.sleep(2.0)
                    
                    # Map the network drive in the CURRENT user session (so it is visible in Explorer)
                    mount_cmd = [
                        "C:\\Windows\\System32\\mount.exe",
                        "-o", "nolock,anon,mtype=hard,rsize=32,wsize=32,timeout=60",
                        "\\\\127.0.0.1\\!",
                        str(mount_point_abs)
                    ]
                    mount_res = subprocess.run(mount_cmd, capture_output=True, text=True)
                    if mount_res.returncode == 0:
                        start_nfs_watcher(str(mount_point_abs), interval=30)
                        print(f"[bold green][OK] NFS mount successfully mapped at [white]{mount_point_abs}[/white]![/bold green]")
                        print("[cyan]Use 'umount' or 'accli mount stop' to unmount the drive.[/cyan]")
                        return
                    else:
                        print(f"[bold red][ERROR] NFS daemon started, but failed to map drive {mount_point_abs} in user session.[/bold red]")
                        if mount_res.stderr:
                            print(f"[red]{mount_res.stderr.strip()}[/red]")
                        print("[yellow]Tip: Make sure Windows NFS client feature was fully enabled and the PC was restarted.[/yellow]")
                        raise typer.Exit(1)
                else:
                    print("[bold red][ERROR] Failed to execute Scheduled Task 'accli-mount-nfs'.[/bold red]")
                    if trigger_res.stderr:
                        print(f"[red]{trigger_res.stderr.strip()}[/red]")
                    print("[yellow]Falling back to UAC elevation...[/yellow]")
                    has_task = False
            except Exception as e:
                if isinstance(e, typer.Exit) or type(e).__name__ == "Exit":
                    raise e
                print(f"[bold red]ERROR: Task Scheduler trigger failed: {e}[/bold red]")
                print("[yellow]Falling back to UAC elevation...[/yellow]")
                has_task = False

        # Fallback or standard spawn path
        try:
            if is_admin:
                process = subprocess.Popen(
                    args,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                print(f"[cyan]Waiting for NFS daemon to initialize...[/cyan]")
                time.sleep(1.0)
                
                # Check if process crashed on boot
                if process.poll() is not None:
                    print(f"[bold red][ERROR] NFS daemon failed to start (exit code {process.returncode}).[/bold red]")
                    try:
                        if Path(log_file_path).exists():
                            with open(log_file_path, "r", encoding="utf-8", errors="ignore") as lf:
                                last_lines = "".join(lf.readlines()[-15:])
                                print("[red]Recent log output:[/red]")
                                print(f"[dim]{last_lines.strip()}[/dim]")
                    except Exception:
                        pass
                    print(f"[yellow]Please check the log file at {log_file_path} for more details.[/yellow]")
                    raise typer.Exit(1)
                    
                start_nfs_watcher(str(mount_point_abs), interval=30)
                print(f"[bold green][OK] NFS mount process spawned successfully (PID: {process.pid}).[/bold green]")
                print(f"[cyan]Use 'umount' or 'accli mount stop' to unmount the drive.[/cyan]")
            else:
                # Running as standard user and Task Scheduler is either not registered or failed
                print("[yellow]Notice: Task Scheduler gateway is not active. Falling back to dynamic UAC elevation...[/yellow]")
                print("[italic]A UAC administrator elevation prompt will appear shortly...[/italic]")
                
                # Formulate environment setup strings for the PowerShell subprocess
                env_setup = ""
                for k in ["ACCELERATOR_MOUNT", "ACC_ENDPOINT", "ACC_CAS_ENDPOINT", "ACC_TOKEN"]:
                    if k in env:
                        val = env[k].replace("'", "''")
                        env_setup += f"$env:{k}='{val}'; "
                
                # Force skip_auto_mount for the elevated daemon
                env_setup += "$env:HF_MOUNT_SKIP_AUTO_MOUNT='1'; "
                
                # Construct powershell command line with single quotes escaped
                ps_args_list = ", ".join(["'" + x.replace("'", "''") + "'" for x in args[1:]])
                ps_command = f"{env_setup}Start-Process -FilePath '{str(hf_mount_bin)}' -ArgumentList @({ps_args_list}) -Verb RunAs"
                
                ps_args = [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                    ps_command
                ]
                
                elevate_res = subprocess.run(ps_args, capture_output=True, text=True)
                if elevate_res.returncode != 0:
                    print("[bold red][ERROR] Administrative elevation failed or was denied.[/bold red]")
                    print("[yellow]Tip: Please request your IT Administrator to run 'setup-nfs.ps1' to configure the passwordless Task Scheduler gateway.[/yellow]")
                    if elevate_res.stderr:
                        print(f"[red]{elevate_res.stderr.strip()}[/red]")
                    raise typer.Exit(1)
                    
                print("[cyan]Waiting for elevated NFS daemon to initialize...[/cyan]")
                time.sleep(2.0)
                
                # Map the network drive in the CURRENT user session (so it is visible in Explorer)
                mount_cmd = [
                    "C:\\Windows\\System32\\mount.exe",
                    "-o", "nolock,anon,mtype=hard,rsize=32,wsize=32,timeout=60",
                    "\\\\127.0.0.1\\!",
                    str(mount_point_abs)
                ]
                mount_res = subprocess.run(mount_cmd, capture_output=True, text=True)
                if mount_res.returncode == 0:
                    start_nfs_watcher(str(mount_point_abs), interval=30)
                    print(f"[bold green][OK] NFS mount successfully mapped at [white]{mount_point_abs}[/white]![/bold green]")
                    print("[cyan]Use 'umount' or 'accli mount stop' to unmount the drive.[/cyan]")
                else:
                    print(f"[bold red][ERROR] NFS daemon spawned elevated, but failed to map drive {mount_point_abs} in user session.[/bold red]")
                    if mount_res.stderr:
                        print(f"[red]{mount_res.stderr.strip()}[/red]")
                    raise typer.Exit(1)
        except Exception as e:
            if isinstance(e, typer.Exit):
                raise e
            print(f"[bold red]ERROR: Failed to spawn Windows mount process: {e}[/bold red]")
            raise typer.Exit(1)

    else:
        # Unix FUSE / NFS Daemonizer Setup
        hf_mount_bin = mount_downloader.get_binary_path("hf-mount")
        if not hf_mount_bin.is_file():
            import shutil
            found_bin = shutil.which("hf-mount")
            if found_bin:
                hf_mount_bin = Path(found_bin)
            else:
                print("[bold red]ERROR: hf-mount binary not found in cache or system PATH.[/bold red]")
                raise typer.Exit(1)

        args = [str(hf_mount_bin), "start"]
        if fuse:
            args.append("--fuse")
            args.append("--advanced-writes")
        
        db_path = get_db_path()
        args.extend(["--token-file", db_path])
        args.extend(["--hub-endpoint", server_url])
        args.extend([mode, project_slug, str(mount_point_abs)])
        
        if overlay:
            args.append("--overlay")
        if read_only:
            args.append("--read-only")

        print(f"[bold cyan]Starting mount for project '[white]{project_slug}[/white]' at '[white]{mount_point_abs}[/white]'...[/bold cyan]")
        import subprocess
        try:
            result = subprocess.run(args, env=env, capture_output=True, text=True)
            if result.returncode == 0:
                print(f"[bold green][OK] Mount started successfully![/bold green]")
                if result.stdout:
                    print(result.stdout)
            else:
                print(f"[bold red][ERROR] Failed to start mount (exit code {result.returncode}):[/bold red]")
                if result.stderr:
                    print(f"[red]{result.stderr}[/red]")
                if result.stdout:
                    print(result.stdout)
                raise typer.Exit(result.returncode)
        except Exception as e:
            print(f"[bold red]ERROR: Failed to execute mount process: {e}[/bold red]")
            raise typer.Exit(1)


@mount_app.command("stop")
def mount_stop(
    mount_point: Annotated[Path, typer.Argument(help="Mount point of the daemon to stop.")] = None,
    binary_version: Annotated[str, typer.Option("--binary-version", "-b", help="Specific hf-mount release version to download.")] = mount_downloader.DEFAULT_VERSION,
):
    """
    Stop a running daemon.
    """
    from . import mount_downloader
    import platform
    import re
    
    sys_name = platform.system()
    
    # Resolve mount point
    if sys_name == "Windows":
        if mount_point is None:
            config_file = Path("C:/ProgramData/accli/mount_config.json")
            mount_point_abs = Path("W:")
            if config_file.is_file():
                try:
                    import json
                    cfg = json.loads(config_file.read_text(encoding="utf-8"))
                    if "mount_point" in cfg:
                        mount_point_abs = Path(cfg["mount_point"])
                except Exception:
                    pass
            print(f"[cyan]No mount point specified. Selected active mount point from config: [bold white]{mount_point_abs}[/bold white][/cyan]")
        else:
            mount_point_str = str(mount_point)
            if not re.match(r"^[a-zA-Z]:/?\\?$", mount_point_str):
                print(f"[bold red]ERROR: Invalid Windows drive letter provided: '{mount_point_str}'.[/bold red]")
                print(f"[yellow]On Windows, the mount point must be a valid drive letter (e.g., 'W:').[/yellow]")
                raise typer.Exit(1)
            
            mount_point_abs = Path(mount_point_str[0].upper() + ":")
            
            # Validate against active config to prevent stopping the wrong mount
            config_file = Path("C:/ProgramData/accli/mount_config.json")
            if config_file.is_file():
                try:
                    import json
                    cfg = json.loads(config_file.read_text(encoding="utf-8"))
                    if "mount_point" in cfg:
                        active_mount = Path(cfg["mount_point"])
                        if str(mount_point_abs).upper() != str(active_mount).upper():
                            print(f"[bold red]ERROR: You requested to stop '{mount_point_abs}', but the active daemon is mounted at '{active_mount}'.[/bold red]")
                            print(f"[yellow]To stop the server, please run: accli mount stop {active_mount}[/yellow]")
                            raise typer.Exit(1)
                except Exception as e:
                    if isinstance(e, typer.Exit) or type(e).__name__ == "Exit":
                        raise e
    else:
        if mount_point is None:
            try:
                project_slug = get_project_slug()
            except Exception:
                project_slug = None
            if not project_slug:
                print("[bold red]ERROR: Mount point not specified and active project slug could not be determined.[/bold red]")
                raise typer.Exit(1)
            mount_point_abs = Path.home() / "accelerator" / "mnt" / project_slug
            print(f"[cyan]No mount point specified. Selected default directory: [bold white]{mount_point_abs}[/bold white][/cyan]")
        else:
            mount_point_abs = mount_point.resolve()

    if sys_name == "Windows":
        print(f"[bold cyan]Stopping mount at '[white]{mount_point_abs}[/white]' on Windows...[/bold cyan]")
        import subprocess
        
        # Determine if current user is admin
        try:
            import ctypes
            is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            is_admin = False

        # 0. Stop background NFS watcher
        stop_nfs_watcher(str(mount_point_abs))

        # 1. Unmap the network drive in the user session
        res = subprocess.run(["C:\\Windows\\System32\\umount.exe", "-f", str(mount_point_abs)], capture_output=True, text=True)
        if res.returncode != 0:
            # Fallback to force-releasing using net use
            subprocess.run(["net", "use", str(mount_point_abs), "/delete", "/y"], capture_output=True)

        # 2. Terminate the background daemon
        has_umount_task = False
        if not is_admin:
            try:
                task_check = subprocess.run(["schtasks", "/query", "/tn", "accli-umount-nfs"], capture_output=True, text=True)
                if task_check.returncode == 0:
                    has_umount_task = True
            except Exception:
                pass

        if has_umount_task:
            print("[cyan]Triggering elevated NFS daemon termination via Task Scheduler...[/cyan]")
            subprocess.run(["schtasks", "/run", "/tn", "accli-umount-nfs"], capture_output=True)
            # Poll for up to 5 seconds to ensure hf-mount-nfs.exe has fully terminated
            for _ in range(10):
                time.sleep(0.5)
                task_check = subprocess.run(["tasklist", "/FI", "IMAGENAME eq hf-mount-nfs.exe"], capture_output=True, text=True)
                if "hf-mount-nfs.exe" not in task_check.stdout:
                    break
        else:
            # If admin or task scheduler gateway is not registered, stop/kill directly
            if is_admin:
                try:
                    subprocess.run(["schtasks", "/end", "/tn", "accli-mount-nfs"], capture_output=True)
                except Exception:
                    pass
                try:
                    escaped_mount_point = str(mount_point_abs).replace("'", "''")
                    kill_cmd = [
                        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                        f"Get-CimInstance Win32_Process -Filter \"name='hf-mount-nfs.exe'\" | "
                        f"Where-Object {{ $_.CommandLine -like '*{escaped_mount_point}*' }} | "
                        f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"
                    ]
                    subprocess.run(kill_cmd, capture_output=True)
                except Exception:
                    # Fallback to killing all instances if selective filter fails
                    subprocess.run(["taskkill", "/f", "/im", "hf-mount-nfs.exe"], capture_output=True)
            else:
                # Standard user with no task registered - need UAC to end task/kill process
                print("[yellow]Notice: Task Scheduler gateway is not active. Requesting UAC elevation to terminate daemon...[/yellow]")
                ps_command = (
                    "Start-Process powershell.exe -ArgumentList '-NoProfile -ExecutionPolicy Bypass -Command "
                    "\"Stop-ScheduledTask -TaskName accli-mount-nfs -ErrorAction SilentlyContinue; "
                    "Get-CimInstance Win32_Process -Filter \\\"name=''hf-mount-nfs.exe''\\\" | Stop-Process -Force -ErrorAction SilentlyContinue\"' "
                    "-Verb RunAs -Wait"
                )
                subprocess.run([
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                    ps_command
                ], capture_output=True)
        
        if res.returncode == 0 or "successfully" in res.stdout or "successfully" in res.stderr:
            print("[bold green][OK] Mount stopped successfully.[/bold green]")
        else:
            print("[yellow]Attempted to unmount. Please verify drive status manually using 'umount'.[/yellow]")
            if res.stderr:
                print(f"[red]{res.stderr.strip()}[/red]")
        return

    # Ensure binaries are downloaded and cached (Unix)
    try:
        mount_downloader.ensure_binaries(version=binary_version)
    except Exception as e:
        print(f"[bold red]ERROR: Binary download/preparation failed: {e}[/bold red]")
        raise typer.Exit(1)

    hf_mount_bin = mount_downloader.get_binary_path("hf-mount")
    if not hf_mount_bin.is_file():
        import shutil
        found_bin = shutil.which("hf-mount")
        if found_bin:
            hf_mount_bin = Path(found_bin)
        else:
            print("[bold red]ERROR: hf-mount binary not found in cache or system PATH.[/bold red]")
            raise typer.Exit(1)

    args = [str(hf_mount_bin), "stop", str(mount_point_abs)]

    print(f"[bold cyan]Stopping mount at '[white]{mount_point_abs}[/white]'...[/bold cyan]")
    
    import subprocess
    try:
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"[bold green][OK] Mount stopped successfully.[/bold green]")
        else:
            print(f"[bold red][ERROR] Failed to stop mount (exit code {result.returncode}):[/bold red]")
            if result.stderr:
                print(f"[red]{result.stderr}[/red]")
            raise typer.Exit(result.returncode)
    except Exception as e:
        print(f"[bold red]ERROR: Failed to execute stop process: {e}[/bold red]")
        raise typer.Exit(1)


@mount_app.command("status")
def mount_status(
    binary_version: Annotated[str, typer.Option("--binary-version", "-b", help="Specific hf-mount release version to download.")] = mount_downloader.DEFAULT_VERSION,
):
    """
    List all running mount daemons.
    """
    from . import mount_downloader
    import platform
    
    sys_name = platform.system()

    if sys_name == "Windows":
        import subprocess
        print("[bold cyan]Active NFS mounts on Windows:[/bold cyan]")
        try:
            # Windows 'mount' command lists active NFS mounts
            result = subprocess.run(["mount"], capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                print(result.stdout)
            else:
                # Check tasklist for active daemon process
                task_res = subprocess.run(["tasklist", "/FI", "IMAGENAME eq hf-mount-nfs.exe"], capture_output=True, text=True)
                if "hf-mount-nfs.exe" in task_res.stdout:
                    print("[cyan]hf-mount-nfs.exe process is running in the background.[/cyan]")
                else:
                    print("[cyan]No running mounts found.[/cyan]")
        except Exception as e:
            print(f"[red]Could not retrieve mount status: {e}[/red]")
        return

    # Ensure binaries are downloaded and cached (Unix)
    try:
        mount_downloader.ensure_binaries(version=binary_version)
    except Exception as e:
        print(f"[bold red]ERROR: Binary download/preparation failed: {e}[/bold red]")
        raise typer.Exit(1)

    hf_mount_bin = mount_downloader.get_binary_path("hf-mount")
    if not hf_mount_bin.is_file():
        import shutil
        found_bin = shutil.which("hf-mount")
        if found_bin:
            hf_mount_bin = Path(found_bin)
        else:
            print("[bold red]ERROR: hf-mount binary not found in cache or system PATH.[/bold red]")
            raise typer.Exit(1)

    args = [str(hf_mount_bin), "status"]

    import subprocess
    active_mounts = []

    # 1. Query hf-mount status directly
    try:
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0:
            stdout = result.stdout.strip()
            if stdout:
                for line in stdout.splitlines():
                    if line.strip():
                        active_mounts.append(line.strip())
    except Exception:
        pass

    # 2. Fallback: parse system mount table on Linux to find localhost NFS or FUSE hf-mounts
    if not active_mounts and platform.system() == "Linux":
        try:
            if os.path.exists("/proc/mounts"):
                with open("/proc/mounts", "r") as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) >= 3:
                            dev, mnt_point, fstype = parts[0], parts[1], parts[2]
                            is_nfs_localhost = (fstype == "nfs" and dev.startswith("127.0.0.1:"))
                            is_fuse_hf = ("hf-mount" in dev or "hf-mount" in fstype)
                            if is_nfs_localhost or is_fuse_hf:
                                active_mounts.append(f"{mnt_point}   ({fstype} via {dev})")
        except Exception:
            pass

    if not active_mounts:
        print("[cyan]No running mounts found.[/cyan]")
    else:
        print("[bold cyan]Active Mounts:[/bold cyan]")
        for m in active_mounts:
            print(m)

