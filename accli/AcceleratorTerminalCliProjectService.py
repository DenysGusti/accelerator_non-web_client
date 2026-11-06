import base64
import json
import os
from urllib.parse import urlencode

import urllib3

from accli.common import todict

ACCLI_DEBUG = os.environ.get('ACCLI_DEBUG', False)


class AccAPIError(Exception):

    def __init__(self, message, response, status_code):
        super().__init__(message)

        self.response = response
        self.status_code = status_code


retries = urllib3.util.Retry(
    total=10,
    backoff_factor=1,
    status_forcelist=[408, 429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS", "PUT"]
)

http_client = urllib3.poolmanager.PoolManager(
    num_pools=20, retries=retries, maxsize=2000, block=True
)

http_client_wo_cert_verification = urllib3.poolmanager.PoolManager(
    cert_reqs="CERT_NONE", num_pools=20, retries=retries, maxsize=2000, block=True
)


class AcceleratorTerminalCliProjectService:
    def __init__(
            self,
            user_token,
            server_url,
            verify_cert=(not ACCLI_DEBUG)
    ):

        self.user_token = user_token
        self.verify_cert = verify_cert

        if self.verify_cert:
            self.http_client = http_client
        else:
            self.http_client = http_client_wo_cert_verification

        self.server_url = server_url
        self.cli_base_url = f"{self.server_url}/api/v1/aterm-cli"
        self.common_request_headers = {
            'x-authorization': user_token
        }

    def http_client_request(self, *args, **kwargs):

        if urllib3.__version__.startswith('1.'):
            if 'json' in kwargs:
                json_dict = kwargs.pop('json')

                assert isinstance(json_dict, dict), (
                    f"urllib request: json keyword argument should be dict."
                )

                encoded_data = json.dumps(json_dict).encode('utf-8')

                kwargs['body'] = encoded_data

        res = self.http_client.request(*args, **kwargs)

        if str(res.status)[0] in ['4', '5']:
            raise AccAPIError(
                f"Accelerator api error:: status_code={res.status} :: response_data={res.data}",
                status_code=res.status,
                response=res
            )

        return res

    def get_file_stat(self, project_slug, filename):

        try:
            res = self.http_client_request(
                "POST",
                f"{self.cli_base_url}/{project_slug}/file-stat/",
                json=dict(filename=filename),
                headers=self.common_request_headers
            )
        except AccAPIError as err:
            if err.status_code == 404:
                return None
            else:
                raise err

        return todict(res.data)

    def get_file_url_from_repo(self, filename, token_pass=""):
        project_slug = filename.split('/')[0]
        res = self.http_client_request(
            "GET",
            f"{self.cli_base_url}/{project_slug}/get-file-download-url/?filename={filename}&token_pass={token_pass}",
            headers=self.common_request_headers
        )
        if res.data:
            return todict(res.data)
        return None

    def get_github_app_token(self, project_slug):

        res = self.http_client_request(
            "GET",
            f"{self.cli_base_url}/{project_slug}/github-app-token/",
            headers=self.common_request_headers
        )

        return todict(res.data)

    def get_jobstore_push_url(self, project_slug, filename):
        try:

            res = self.http_client_request(
                "GET",
                f"{self.cli_base_url}/{project_slug}/jobstore-push-url/?filename={filename}",
                headers=self.common_request_headers
            )

        except AccAPIError as err:
            if err.status_code == 409 or err.status_code == '409':
                return None
            else:
                raise err

        return todict(res.data)

    def dispatch(self, project_slug, job_description):

        try:
            res = self.http_client_request(
                "POST",
                f"{self.cli_base_url}/{project_slug}/jobs/dispatch/",
                json=job_description,
                headers=self.common_request_headers
            )
        except AccAPIError as err:
            raise err

        return todict(res.data)['job_id']

    def list_root_jobs(self, project_slug, limit=None, cursor=None, status=None):
        params = dict(limit=limit, cursor=cursor, status=status)
        query = urlencode({k: v for k, v in params.items() if v is not None})
        res = self.http_client_request(
            "GET",
            f"{self.cli_base_url}/{project_slug}/jobs/?{query}",
            headers=self.common_request_headers
        )
        return todict(res.data)

    def get_job_graph(self, project_slug, job_id):
        res = self.http_client_request(
            "GET",
            f"{self.cli_base_url}/{project_slug}/jobs/{job_id}/graph/",
            headers=self.common_request_headers
        )
        return todict(res.data)

    def restart_job(self, project_slug, job_id, rebuild_container_image=False):
        query = urlencode({"rebuild_container_image": "true"}) if rebuild_container_image else ""
        url = f"{self.cli_base_url}/{project_slug}/jobs/restart/{job_id}/"
        if query:
            url = f"{url}?{query}"
        res = self.http_client_request(
            "GET",
            url,
            headers=self.common_request_headers
        )
        return todict(res.data)

    def get_job_logs(self, project_slug, job_id, sources=None, attempt=None, before=None, after=None, limit=None):
        params = dict(sources=sources, attempt=attempt, before=before, after=after, limit=limit)
        query = urlencode({k: v for k, v in params.items() if v is not None})
        res = self.http_client_request(
            "GET",
            f"{self.cli_base_url}/{project_slug}/jobs/{job_id}/logs/?{query}",
            headers=self.common_request_headers
        )
        return todict(res.data)

    def get_log_chunk_text(self, url):
        # Presigned S3 URL: no Accelerator auth header.
        res = self.http_client.request("GET", url)
        if res.status >= 400:
            raise AccAPIError(
                f"Log chunk download failed :: status_code={res.status}",
                status_code=res.status,
                response=res
            )
        return res.data.decode("utf-8", errors="replace")

    def get_dataset_template_details(self, project_slug, template_slug):

        try:
            res = self.http_client_request(
                "GET",
                f"{self.server_url}/api/v1/projects/{project_slug}/dataset-templates/{template_slug}/by-slug/",
            )
        except AccAPIError as err:
            if err.status_code == 404:
                return None
            else:
                raise err

        return todict(res.data)

    def enumerate_files_by_prefix(self, prefix, token_pass=""):
        project_slug = prefix.split('/')[0]

        b64_encoded_prefix = base64.b64encode(prefix.encode()).decode()

        res = self.http_client_request(
            "GET",
            f"{self.cli_base_url}/{project_slug}/enumerate-all-files/{b64_encoded_prefix}/?token_pass={token_pass}",
            headers=self.common_request_headers
        )
        if res.data:
            return todict(res.data)
        return None
