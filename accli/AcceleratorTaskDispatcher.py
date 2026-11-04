import os
import zipfile
import hashlib
import shutil
import tempfile
import fnmatch
import concurrent.futures
import json
from pathlib import Path
from typing import Optional, Dict, List

import requests
from functools import lru_cache
from pydantic import BaseModel, model_validator, Field
from accli.AcceleratorTerminalCliProjectService import AcceleratorTerminalCliProjectService
from accli.token import (
    get_token, get_server_url,
    get_project_slug, exchange_refresh_token
)

ACCLI_DEBUG = os.environ.get('ACCLI_DEBUG', False)

FOLDER_JOB_REPO_URL = 'https://github.com/IIASA-Accelerator/wkube-job.git'


def compress_folder(folder_path, output_path):
    # Fixed timestamp for normalization
    fixed_time = (1980, 1, 1, 0, 0, 0)

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(folder_path):
            dirs.sort()
            files.sort()
            for file in files:
                file_path = os.path.join(root, file)
                arc_name = os.path.relpath(file_path, start=folder_path)
                # Add a file to zip with a fixed timestamp
                info = zipfile.ZipInfo(arc_name)
                info.date_time = fixed_time
                info.external_attr = 0o600 << 16  # Set file permissions
                with open(file_path, 'rb') as f:
                    zipf.writestr(info, f.read(), zipfile.ZIP_DEFLATED)


def get_file_sha1(file_path):
    sha1_hash = hashlib.sha1()
    with open(file_path, "rb") as f:
        chunk = f.read(8192)
        while chunk:
            sha1_hash.update(chunk)
            chunk = f.read(8192)
    return sha1_hash.hexdigest()


def copy_tree(src, dst, include=None, exclude=None, root_src=None):
    """Recursively copy from src to dst, excluding .git folders. Includes override excludes."""
    if root_src is None:
        root_src = src
        
    excludes = [e.strip() for e in exclude.split(',') if e.strip()] if exclude else []
    includes = [i.strip() for i in include.split(',') if i.strip()] if include else []
        
    for item in os.listdir(src):
        src_path = os.path.join(src, item)
        dst_path = os.path.join(dst, item)
        rel_path = os.path.relpath(src_path, root_src)

        if os.path.isdir(src_path):
            if item == '.git':
                continue
            
            is_excluded = any(fnmatch.fnmatch(rel_path, p) or fnmatch.fnmatch(item, p) for p in excludes)
            # Traverse even if excluded as long as includes exist, to find exceptions
            if is_excluded and not includes:
                continue
                
            os.makedirs(dst_path, exist_ok=True)
            copy_tree(src_path, dst_path, include, exclude, root_src)
        else:
            is_excluded = any(fnmatch.fnmatch(rel_path, p) or fnmatch.fnmatch(item, p) for p in excludes)
            is_included = any(fnmatch.fnmatch(rel_path, p) or fnmatch.fnmatch(item, p) for p in includes)
            
            if is_included:
                pass # Included files override exclude
            elif is_excluded:
                continue # Skipped by exclude
            elif includes and not excludes:
                continue # If ONLY includes list is provided, act as whitelist
                
            shutil.copy2(src_path, dst_path)


def _upload_cas_file_worker(args):
    rel_path, file_path, term_cli_project_service, project_slug = args
    file_hash = get_file_sha1(file_path)
    blob_name = f"cas_{file_hash}.blob"
    
    print(f"Checking {rel_path} (hash: {file_hash[:8]})...")
    presigned_push_url = term_cli_project_service.get_jobstore_push_url(
        project_slug, blob_name
    )
    if presigned_push_url:
        print(f"Uploading {rel_path}...")
        with open(file_path, 'rb') as f:
            data = f.read()
            res = requests.put(
                presigned_push_url,
                data=data,
                headers={"Content-Length": str(len(data))},
                verify=False,
            )
            res.raise_for_status()
        print(f"Uploaded {rel_path}.")
    else:
        print(f"Skipped {rel_path} (already in jobstore).")
    
    mode = os.stat(file_path).st_mode
    return {"path": rel_path, "hash": file_hash, "mode": mode}


@lru_cache(maxsize=None)
def push_folder_job(directory, include=None, exclude=None):
    server_url = get_server_url()
    project_slug = get_project_slug()
    _, access_token, _ = exchange_refresh_token(project_slug)

    term_cli_project_service = AcceleratorTerminalCliProjectService(
        user_token=access_token,
        server_url=server_url,
        verify_cert=(not ACCLI_DEBUG)
    )

    repo_dir = tempfile.mkdtemp()
    copy_tree(directory, repo_dir, include, exclude)

    if os.path.isfile(f'{repo_dir}/wkube.py'):
        os.remove(f'{repo_dir}/wkube.py')

    manifest = {"type": "cas_manifest", "files": []}
    
    upload_args = []
    for root, dirs, files in os.walk(repo_dir):
        for file in files:
            file_path = os.path.join(root, file)
            rel_path = Path(file_path).relative_to(repo_dir).as_posix()
            upload_args.append((rel_path, file_path, term_cli_project_service, project_slug))
            
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        for result in executor.map(_upload_cas_file_worker, upload_args):
            manifest["files"].append(result)
            
    manifest_path = os.path.join(repo_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f)
        
    manifest_hash = get_file_sha1(manifest_path)
    manifest_name = f"manifest_{manifest_hash}.json"
    
    presigned_push_url = term_cli_project_service.get_jobstore_push_url(
        project_slug, manifest_name
    )
    
    if presigned_push_url:
        with open(manifest_path, 'rb') as f:
            res = requests.put(
                presigned_push_url,
                data=f,
                verify=False,
            )
            res.raise_for_status()

    shutil.rmtree(repo_dir)
    return f"s3accjobstore://{manifest_name}", manifest_hash


class JobDispatchModel(BaseModel):
    is_holder_job: bool = True
    build_only_task: bool = False

    name: str

    execute_cluster: str
    job_location: str
    job_args: list
    job_kwargs: dict

    required_cores: Optional[float] = None
    required_ram: Optional[float] = None
    required_storage_local: Optional[float] = None

    # is ignored if it is a callback and child of non-free node jobs
    required_storage_workflow: Optional[float] = None

    job_secrets: Optional[dict] = {}

    timeout: Optional[int] = None
    pvc_id: Optional[str] = None
    node_id: Optional[str] = None

    ignore_duplicate_job: bool = False
    free_node: bool = True

    children: List['JobDispatchModel'] = []
    callback: Optional['JobDispatchModel'] = None


class WKubeTaskMeta(BaseModel):
    required_cores: float
    required_ram: float
    required_storage_local: Optional[float] = None

    # is ignored if it is a callback and child of non-free node jobs
    required_storage_workflow: Optional[float] = None

    job_secrets: Optional[dict] = {}

    timeout: int


class WKubeTaskKwargs(BaseModel):
    docker_image: Optional[str] = None

    job_folder: str = Field('./', description="Path to the directory containing job files. Must be a valid existing directory.")
    include: Optional[str] = Field(None, description="Comma-separated wildcard patterns to include (e.g. '*.py, data/*')")
    exclude: Optional[str] = Field(None, description="Comma-separated wildcard patterns to exclude (e.g. '*.txt, .venv/*')")

    repo_url: Optional[str] = None  # required when docker image is not present
    repo_branch: Optional[str] = None  # required when docker image is not present

    docker_filename: Optional[str] = None  # when not docker image;
    base_stack: Optional[str] = None  # when not github docker file #TODO add enum class of available base stack

    force_build: bool = False

    command: str  # may not be present with docker_image # TODO wrap a command in custom script to implement timeout or possibly log ingestion if required.

    conf: Dict[str, str] = {}

    build_timeout: Optional[int] = None

    def model_dump(self, *args, **kwargs):
        result = super().model_dump(*args, **kwargs)
        if 'job_folder' in result:
            del result['job_folder']
        if 'include' in result:
            del result['include']
        if 'exclude' in result:
            del result['exclude']
        return result

    @model_validator(mode="before")
    @classmethod
    def validate_root(cls, values):
        if not values.get('docker_image'):
            job_folder = values.get('job_folder', './')
            include = values.get('include')
            exclude = values.get('exclude')

            if not os.path.isdir(job_folder):
                raise ValueError(
                    f"Invalid job_folder: '{job_folder}' does not exist or is not a directory. "
                    f"Help: job_folder must be a valid path. You can also use 'include' and 'exclude' "
                    f"with comma-separated wildcards (e.g., include='*.py, data/*', exclude='*.txt, .venv/*')."
                )

            if not (values.get('repo_url') and values.get('repo_branch')):
                remote_url, branch_name = push_folder_job(
                    os.path.abspath(job_folder), include, exclude
                )
                values['repo_url'] = remote_url
                values['repo_branch'] = branch_name
            else:
                if not values.get('repo_url'):
                    # TODO if has no repo url just set it
                    raise ValueError("repo_url is required")

                if not values.get('repo_branch'):
                    raise ValueError('repo_branch is required')

            if not values.get('docker_filename'):
                if not values.get("base_stack"):
                    raise ValueError("base_stack is required when dockerfile is not defined")
        return values


class WKubeTaskPydantic(WKubeTaskMeta, WKubeTaskKwargs):
    pass


class GenericTask:
    def __init__(self, *args, **kwargs):
        self.dispatch_model_task: JobDispatchModel

    def add_child(self, task):
        if self.__class__ != task.__class__:
            raise ValueError(f"task should of {self.__class__} class")
        self.dispatch_model_task.children.append(task.dispatch_model_task)

    def add_callback(self, task):
        if self.__class__ != task.__class__:
            raise ValueError(f"task should of {self.__class__} class")
        self.dispatch_model_task.callback = task.dispatch_model_task

    @property
    def description(self):
        return self.dispatch_model_task.model_dump()


class WKubeTask(GenericTask):
    def __init__(self, *t_args, **t_kwargs):
        super().__init__(*t_args, **t_kwargs)
        wkube_task_kwargs = None
        wkube_task_meta = {}

        name = t_kwargs.pop('name') if 'name' in t_kwargs else None
        if t_args or t_kwargs:
            WKubeTaskPydantic(*t_args, **t_kwargs)
            wkube_task_kwargs = WKubeTaskKwargs(*t_args, **t_kwargs)
            wkube_task_meta.update(WKubeTaskMeta(*t_args, **t_kwargs).model_dump(exclude_unset=True))

        self.dispatch_model_task = JobDispatchModel(
            name=name,
            is_holder_job=not (t_args or t_kwargs),
            execute_cluster='WKUBE',
            job_location='acc_native_jobs.dispatch_wkube_task',
            job_args=[],
            job_kwargs=wkube_task_kwargs.model_dump(exclude_unset=True) if wkube_task_kwargs else {},
            **wkube_task_meta
        )
