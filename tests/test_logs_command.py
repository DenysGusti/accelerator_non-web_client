"""`accli logs` against a fake logs API: tail, follow, partial lines and late events."""

from typer.testing import CliRunner

from accli import cli


class FakeService:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_job_logs(self, project_slug, job_id, **params):
        self.calls.append(params)
        return self.pages.pop(0)

    def get_log_chunk_text(self, url):
        return TEXTS[url]


TEXTS = {
    "u0": "2026-10-29T09:30:01.000Z\tstarting\n2026-10-29T09:30:02.000Z\tdownloading 4",
    "u1": "0%\n2026-10-29T09:30:15.000Z\tdone\n",
}


def page(chunks, events=(), completed=False, after="next"):
    return dict(chunks=list(chunks), events=list(events), before=None, after=after,
                has_more_before=False, job_completed=completed)


def chunk(seq, url, end):
    return dict(source="wkube", seq=seq, url=url, start=None, end=end, size=1)


def run(monkeypatch, pages, *args):
    service = FakeService(pages)
    monkeypatch.setattr(cli, "exchange_refresh_token", lambda slug: ("", "token", 0))
    monkeypatch.setattr(cli, "validate_project_slug", lambda server_url, slug: None)
    monkeypatch.setattr(cli, "get_server_url", lambda: "https://acc.example")
    monkeypatch.setattr(cli, "AcceleratorTerminalCliProjectService", lambda **kw: service)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    result = CliRunner().invoke(cli.app, ["logs", "demo", "42", "--no-time", *args])
    assert result.exit_code == 0, result.output
    return result.output.splitlines(), service


def test_follow_holds_back_partial_line_and_stops_when_done(monkeypatch):
    late = dict(id="e1", time="2026-10-29T09:30:10Z", precise=False, kind="Pod", reason="Started", message="")
    output, service = run(monkeypatch, [
        page([chunk(0, "u0", "2026-10-29T09:30:10Z")]),
        page([chunk(1, "u1", "2026-10-29T09:30:20Z")], events=[late], completed=True),
    ], "-f")

    assert [line.split(None, 1)[1] for line in output] == [
        "starting",
        "downloading 40%",  # started at 09:30:02, printed only once complete
        "Pod · Started",
        "done",
    ]
    assert service.calls[1]["after"] == "next"


def test_without_follow_prints_partial_line_once(monkeypatch):
    output, service = run(monkeypatch, [page([chunk(0, "u0", "2026-10-29T09:30:10Z")])])

    assert [line.split(None, 1)[1] for line in output] == ["starting", "downloading 4"]
    assert len(service.calls) == 1

