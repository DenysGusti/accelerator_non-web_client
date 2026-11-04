"""Rendering for `accli jobs` and `accli job`."""
from datetime import datetime

from rich.markup import escape
from rich.table import Table
from rich.text import Text

FAILED_STATUSES = {
    "DISPATCH_TO_EXECUTOR_FAILED",
    "QUEUE_TIMEOUT",
    "PREPARING_TIMEOUT",
    "ERROR",
    "TIMEOUT",
    "KILLED",
}
FINISHED_STATUSES = FAILED_STATUSES | {"DONE"}

def status_style(status):
    if status == "DONE":
        return "green"
    if status in FAILED_STATUSES:
        return "red"
    if status == "PROCESSING":
        return "cyan"
    return "yellow"


def status_text(status):
    status = status or "-"
    return Text(status, style=status_style(status))


def job_type(job):
    """Same wording as the web UI's job card."""
    has_children = job.get("children_count", 0) > 0
    has_callback = bool(job.get("immediate_callback"))
    if has_children and has_callback:
        return "children+callback"
    if has_children:
        return "children"
    if has_callback:
        return "callback"
    return "standalone"


def format_time(value):
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def job_title(job):
    return job.get("name") or job.get("job_entry_location") or ""


def root_jobs_table(jobs):
    table = Table(box=None, header_style="bold", pad_edge=False)
    table.add_column("ID", justify="right", style="bold")
    table.add_column("NAME", overflow="fold")
    table.add_column("TYPE")
    table.add_column("EXECUTOR")
    table.add_column("STATUS")
    table.add_column("GRAPH")
    table.add_column("CREATED")
    table.add_column("BY")

    for job in jobs:
        graph_status = job.get("job_graph_status")
        table.add_row(
            str(job["id"]),
            escape(job_title(job)),
            job_type(job),
            job["job_executor_id"],
            status_text(job["status"]),
            status_text(graph_status) if graph_status else Text("-", style="dim"),
            format_time(job.get("created_at")),
            escape(job.get("dispatcher_full_name") or ""),
        )
    return table


def _links(graph):
    """({job id: children}, {job id: callbacks}) within the graph."""
    ids = {job["id"] for job in graph["jobs"]}
    children, callbacks = {}, {}
    for job in graph["jobs"]:
        if job.get("parent_job_id") in ids:
            children.setdefault(job["parent_job_id"], []).append(job)
        elif job.get("predecessor_job_id") in ids:
            callbacks.setdefault(job["predecessor_job_id"], []).append(job)
    return children, callbacks


def short_kind(job, graph):
    if job["id"] == graph["root_job_id"]:
        return "root"
    return "child" if job.get("parent_job_id") else "callback"


def kind_of(job, graph):
    if job["id"] == graph["root_job_id"]:
        return "root"
    if job.get("parent_job_id"):
        return f"child of #{job['parent_job_id']}"
    return f"callback after #{job['predecessor_job_id']}"


def _rows(graph, top_id):
    """Yield (job, kind, tree_prefix) for top_id and everything it holds, depth first.

    Children nest one level under the job holding them. A callback sits on the same level as
    the job it runs after, right below that job's block, marked with ↳.
    """
    jobs = {job["id"]: job for job in graph["jobs"]}
    children, callbacks = _links(graph)

    def sequence(job):
        """The job followed by its callback chain."""
        return [job] + [then for callback in callbacks.get(job["id"], []) for then in sequence(callback)]

    def emit(job, prefix, nested):
        kind = short_kind(job, graph)
        # The top row has nothing above it to follow, so it gets no arrow.
        yield job, kind, prefix + ("↳ " if kind == "callback" and job["id"] != top_id else "")
        held = [then for child in children.get(job["id"], []) for then in sequence(child)]
        for i, held_job in enumerate(held):
            last = i == len(held) - 1
            yield from emit(held_job, nested + ("└─ " if last else "├─ "), nested + ("   " if last else "│  "))

    for job in sequence(jobs[top_id]):
        yield from emit(job, "", "")


def subtree(graph, job_id):
    """The job and every child and callback below it."""
    return [job for job, _, _ in _rows(graph, job_id)]


def combined_status(jobs):
    """PROCESSING while any job is unfinished, else ERROR if any failed, else DONE."""
    statuses = [job.get("status") for job in jobs]
    if any(s not in FINISHED_STATUSES for s in statuses):
        return "PROCESSING"
    if any(s in FAILED_STATUSES for s in statuses):
        return "ERROR"
    return "DONE"


def status_counts(jobs):
    counts = {}
    for job in jobs:
        status = job.get("status") or "-"
        counts[status] = counts.get(status, 0) + 1
    text = Text()
    for i, (status, count) in enumerate(sorted(counts.items(), key=lambda kv: -kv[1])):
        if i:
            text.append(", ")
        text.append(f"{count} ")
        text.append_text(status_text(status))
    return text


def _own_status(job):
    text = status_text(job.get("status"))
    if job.get("status_at"):
        text.append(f"  since {format_time(job['status_at'])}", style="dim")
    return text


def job_details(graph, job_id, project_slug):
    """Key/value block for one job. Graph status covers the jobs below it, when it has any."""
    job = next(j for j in graph["jobs"] if j["id"] == job_id)
    jobs = subtree(graph, job_id)

    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()

    table.add_row("Status", _own_status(job))
    below = jobs[1:]
    if below:
        # Same rule as the GRAPH column of `accli jobs`: only the jobs below, not this one.
        graph_status = status_text(combined_status(below))
        graph_status.append(f"  {len(below)} jobs below: ", style="dim")
        graph_status.append_text(status_counts(below))
        table.add_row("Graph status", graph_status)

    table.add_row("Executor", job["job_executor_id"] + (f"  ({job['executor_job_id']})" if job.get("executor_job_id") else ""))
    table.add_row("Created", format_time(job.get("created_at")))
    table.add_row("Kind", kind_of(job, graph))

    if job_id != graph["root_job_id"]:
        root = next(j for j in graph["jobs"] if j["id"] == graph["root_job_id"])
        part_of = Text()
        part_of.append(f"#{root['id']} {job_title(root)}", style="bold")
        part_of.append("  ")
        part_of.append_text(status_text(graph["graph_status"]))
        part_of.append(f"  {len(graph['jobs'])} jobs  ", style="dim")
        part_of.append(f"accli job {project_slug} {root['id']}", style="cyan")
        table.add_row("Part of", part_of)

    table.add_row("Logs", Text(f"accli logs {project_slug} {job['id']}", style="cyan"))

    title = Text()
    title.append(f"Job #{job['id']}", style="bold")
    title.append(f"  {job_title(job)}")
    return title, table


def jobs_table(graph, top_id):
    """top_id and every job it holds as aligned rows; the tree is drawn in the first column."""
    table = Table(box=None, header_style="bold", pad_edge=False)
    table.add_column("JOB", no_wrap=True)
    table.add_column("KIND", style="dim")
    table.add_column("STATUS")
    table.add_column("EXECUTOR", style="dim")
    table.add_column("UPDATED", style="dim")

    for job, kind, prefix in _rows(graph, top_id):
        cell = Text(prefix, style="dim")
        cell.append(f"#{job['id']}", style="bold")
        cell.append(f" {job_title(job)}")
        table.add_row(
            cell,
            kind,
            status_text(job.get("status")),
            job["job_executor_id"],
            format_time(job.get("status_at")),
        )
    return table
