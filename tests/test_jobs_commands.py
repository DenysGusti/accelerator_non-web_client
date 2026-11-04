"""`accli jobs` and `accli job` against a fake API."""
import json

from typer.testing import CliRunner

from accli import cli
from accli.AcceleratorTerminalCliProjectService import AccAPIError


def job(id, name, status="DONE", children=0, callback=False, graph=None):
    return dict(id=id, name=name, job_executor_id="WKUBE", job_entry_location="main.py", status=status,
                job_graph_status=graph, children_count=children, immediate_callback=callback,
                dispatcher_full_name="Ada Lovelace", created_at="2026-11-01T10:00:00Z")


class FakeService:
    def __init__(self, pages=(), graph=None, error=None):
        self.pages = list(pages)
        self.graph = graph
        self.error = error
        self.calls = []

    def list_root_jobs(self, project_slug, **params):
        self.calls.append(params)
        if self.error:
            raise self.error
        return self.pages.pop(0)

    def get_job_graph(self, project_slug, job_id):
        self.calls.append(dict(job_id=job_id))
        return self.graph


def run(monkeypatch, service, *args, exit_code=0):
    monkeypatch.setattr(cli, "exchange_refresh_token", lambda slug: ("", "token", 0))
    monkeypatch.setattr(cli, "get_server_url", lambda: "https://acc.example")
    monkeypatch.setattr(cli, "AcceleratorTerminalCliProjectService", lambda **kw: service)
    result = CliRunner().invoke(cli.app, [*args, "-p", "demo"], env={"COLUMNS": "200"})
    assert result.exit_code == exit_code, result.output
    return result.output


def test_jobs_lists_types_and_joins_status_filters(monkeypatch):
    service = FakeService([dict(count=3, next_cursor="c1", results=[
        job(3, "workflow", "PROCESSING", children=2, callback=True, graph="PROCESSING"),
        job(2, "single", "ERROR"),
        job(1, "with cb", callback=True, graph="DONE"),
    ])])
    output = run(monkeypatch, service, "jobs", "-s", "error,processing", "--status", "done", "-n", "3")

    assert service.calls == [dict(limit=3, cursor=None, status="error,processing,done")]
    rows = {line.split()[0]: line for line in output.splitlines() if line.strip()[:1].isdigit()}
    assert "children+callback" in rows["3"] and "PROCESSING" in rows["3"]
    assert "standalone" in rows["2"] and "ERROR" in rows["2"]
    assert "callback" in rows["1"]
    assert "Showing" not in output


def test_jobs_all_follows_cursor(monkeypatch):
    service = FakeService([
        dict(count=2, next_cursor="c1", results=[job(2, "b")]),
        dict(count=2, next_cursor=None, results=[job(1, "a")]),
    ])
    output = json.loads(run(monkeypatch, service, "jobs", "--all", "--json"))

    assert [j["id"] for j in output["results"]] == [2, 1]
    assert service.calls[1]["cursor"] == "c1"


def test_jobs_shows_api_error(monkeypatch):
    class Res:
        data = b'{"detail": "Unknown graph status \'NOPE\'."}'

    service = FakeService(error=AccAPIError("bad", Res(), 422))
    output = run(monkeypatch, service, "jobs", "-s", "nope", exit_code=1)
    assert "Unknown graph status 'NOPE'" in output


def node(id, name, status, parent=None, predecessor=None):
    return dict(id=id, name=name, job_executor_id="WKUBE", job_entry_location="x", status=status,
                parent_job_id=parent, predecessor_job_id=predecessor, status_at=None, created_at=None)


GRAPH = dict(root_job_id=10, graph_status="ERROR", jobs=[
    node(10, "root", "DONE"),
    node(11, "child a", "DONE", parent=10),
    node(12, "child b", "ERROR", parent=10),
    node(13, "after a", "DONE", predecessor=11),
    node(14, "root callback", "KILLED", predecessor=10),
])


def test_leaf_job_shows_only_its_own_status(monkeypatch):
    output = run(monkeypatch, FakeService(graph=GRAPH), "job", "13")

    assert output.startswith("Job #13  after a")
    assert "Status    DONE" in output and "Graph status" not in output
    assert "callback after #11" in output
    assert "Part of   #10 root  ERROR  5 jobs  accli job 10" in output
    assert "accli logs 13" in output
    assert "Jobs in" not in output


def test_parent_job_shows_graph_status_of_jobs_below(monkeypatch):
    output = run(monkeypatch, FakeService(graph=GRAPH), "job", "11")
    details, jobs = output.split("\nJobs in #11\n")

    # Below #11 is only its callback #13; #12 and #14 belong to the root.
    assert "Status        DONE" in details
    assert "Graph status  DONE  1 jobs below: 1 DONE" in details
    rows = [line.rstrip() for line in jobs.splitlines()[1:] if line.strip()]
    assert [row.split("#")[1].split()[0] for row in rows] == ["11", "13"]
    assert rows[1].startswith("↳ #13 after a")  # callback: same level as #11, right after it


def test_root_job_lists_whole_pipeline(monkeypatch):
    output = run(monkeypatch, FakeService(graph=GRAPH), "job", "10")
    details, jobs = output.split("\nJobs in #10\n")

    assert "Status        DONE" in details
    assert "Graph status  ERROR  4 jobs below: 2 DONE, 1 ERROR, 1 KILLED" in details
    assert "Kind          root" in details and "Part of" not in details
    rows = [line.rstrip() for line in jobs.splitlines()[1:] if line.strip()]
    # A callback sits on the level of the job it runs after, right after that job.
    assert [row.split("#")[1].split()[0] for row in rows] == ["10", "11", "13", "12", "14"]
    assert rows[0].startswith("#10 root")
    assert rows[1].startswith("├─ #11 child a")
    assert rows[2].startswith("├─ ↳ #13 after a")
    assert rows[3].startswith("└─ #12 child b")
    assert rows[4].startswith("↳ #14 root callback")
    assert [row.split()[-4] for row in rows] == ["root", "child", "callback", "child", "callback"]


def test_graph_status_leaves_out_the_job_itself(monkeypatch):
    # A killed root whose jobs below all finished: GRAPH in `accli jobs` says DONE, so must this.
    graph = dict(root_job_id=1, graph_status="DONE", jobs=[
        node(1, "root", "KILLED"), node(2, "child", "DONE", parent=1)])
    output = run(monkeypatch, FakeService(graph=graph), "job", "1")
    assert "Status        KILLED" in output
    assert "Graph status  DONE  1 jobs below: 1 DONE" in output
