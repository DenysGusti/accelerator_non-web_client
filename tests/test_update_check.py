"""Verify that `check_for_updates` runs via the root callback for all CLI commands with a unified design."""
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from accli import cli
from accli._version import VERSION


class _FakeService:
    def get_job_logs(self, *args, **kwargs):
        return dict(chunks=[], events=[], after=None, before=None, has_more_before=False, job_completed=True)

    def list_root_jobs(self, *args, **kwargs):
        return dict(results=[], count=0)

    def get_job_graph(self, *args, **kwargs):
        return dict(
            root_job_id=1,
            graph_status="DONE",
            jobs=[
                dict(
                    id=1,
                    name="root",
                    job_executor_id="WKUBE",
                    job_entry_location="main.py",
                    status="DONE",
                    parent_job_id=None,
                    predecessor_job_id=None,
                    status_at=None,
                    created_at=None,
                )
            ],
        )


ALL_COMMAND_INVOCATIONS = [
    [],
    ["-h"],
    ["--help"],
    ["--version"],
    ["-v"],
    ["about"],
    ["gui"],
    ["login"],
    ["validate", "demo", "tmpl", "file.csv"],
    ["dispatch", "demo", "root_task", "-f", "nonexistent_wkube.py"],
    ["logs", "demo", "1"],
    ["jobs", "demo"],
    ["job", "demo", "1"],
    ["copy", "local_a", "local_b"],
    ["mount", "start", "demo", "--mode", "invalid"],
    ["mount", "stop", "invalid_drive"],
    ["mount", "status"],
]


class TestUpdateCheckAllCommands(unittest.TestCase):
    def setUp(self):
        import accli.gui

        class _FakeResp:
            status_code = 200

            @staticmethod
            def json():
                return {"access_token": "tok", "refresh_token": "ref"}

        self.patches = [
            patch.object(accli.gui, "main", lambda: None),
            patch.object(cli.typer, "prompt", lambda *a, **kw: "fake-code"),
            patch.object(cli.requests, "post", lambda *a, **kw: _FakeResp()),
            patch.object(cli, "save_token_details", lambda *a, **kw: None),
            patch.object(cli, "AcceleratorTerminalCliProjectService", lambda **kw: _FakeService()),
            patch.object(cli, "CsvRegionalTimeseriesValidator", lambda **kw: (lambda: None)),
            patch.object(cli, "_cli_service_for", lambda slug: _FakeService()),
            patch.object(cli, "get_token", lambda: ""),
            patch.object(cli, "get_server_url", lambda: "https://acc.example"),
            patch.object(cli, "set_project_slug", lambda slug: None),
            patch.object(cli, "exchange_refresh_token", lambda slug: ("", "token", 0)),
            patch("platform.system", lambda: "Windows"),
            patch.object(cli, "is_nfs_daemon_running", lambda: False),
            patch.object(cli, "stop_nfs_watcher", lambda *a, **kw: None),
            patch.object(cli, "is_windows_drive_mapped", lambda *a, **kw: False),
            patch.object(cli, "cleanup_stuck_windows_mount_processes", lambda: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_all_registered_commands_are_covered(self):
        """Ensure every command registered on `app` and `mount_app` is included in ALL_COMMAND_INVOCATIONS."""
        root_commands = {cmd.name or cmd.callback.__name__ for cmd in cli.app.registered_commands}
        mount_commands = {cmd.name or cmd.callback.__name__ for cmd in cli.mount_app.registered_commands}

        tested_root = {args[0] for args in ALL_COMMAND_INVOCATIONS if args and not args[0].startswith("-")}
        tested_mount = {args[1] for args in ALL_COMMAND_INVOCATIONS if args[:1] == ["mount"] and len(args) > 1}

        self.assertTrue(root_commands <= tested_root, f"Missing root commands: {root_commands - tested_root}")
        self.assertTrue(mount_commands <= tested_mount, f"Missing mount commands: {mount_commands - tested_mount}")

    def test_update_notice_shown_once_on_every_command(self):
        runner = CliRunner()
        expected_banner = f"🔔 Update available: accli 99.0.0 (current: {VERSION})"
        expected_instruction = "To update, run: pip install -U accli"

        for args in ALL_COMMAND_INVOCATIONS:
            with self.subTest(command=" ".join(args)):
                calls = []

                def fake_check():
                    calls.append(1)
                    return True, "99.0.0"

                with patch.object(cli, "check_for_updates", fake_check), patch.object(
                    cli, "get_upgrade_instruction", lambda: expected_instruction
                ):
                    result = runner.invoke(cli.app, args, env={"COLUMNS": "200"})

                self.assertEqual(
                    len(calls),
                    1,
                    f"Expected check_for_updates to be called once for {args}, got {len(calls)}",
                )
                self.assertEqual(result.output.count(expected_banner), 1, result.output)
                self.assertEqual(result.output.count(expected_instruction), 1, result.output)

    def test_no_update_notice_when_up_to_date_on_every_command(self):
        runner = CliRunner()

        for args in ALL_COMMAND_INVOCATIONS:
            with self.subTest(command=" ".join(args)):
                calls = []

                def fake_check():
                    calls.append(1)
                    return False, None

                with patch.object(cli, "check_for_updates", fake_check):
                    result = runner.invoke(cli.app, args, env={"COLUMNS": "200"})

                self.assertEqual(len(calls), 1, f"Expected check_for_updates to be called once for {args}")
                self.assertNotIn("Update available:", result.output)


if __name__ == "__main__":
    unittest.main()
