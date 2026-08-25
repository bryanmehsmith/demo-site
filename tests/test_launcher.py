"""Tests for launcher.py, using only the standard library.

demo-site has no pyproject and no Python dependencies, so these run with
`python -m unittest discover tests` and nothing to install.

The reaper's decision table is the part worth testing: it is a few lines of
conditionals that decide whether to kill a visitor's session, and getting it
wrong is either a visible outage or a memory leak that only shows up days later.
Process handling is faked so the tests stay fast and do not spawn Streamlit.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import launcher  # noqa: E402


class FakeProcess:
    """Stands in for subprocess.Popen: alive until terminated."""

    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode

    def kill(self):
        self.returncode = -9


def make_demo(monkey, port=8502, cpu=0.0, connections=0):
    """A Demo with a live fake process, and the /proc lookups stubbed."""
    demo = launcher.Demo({"slug": "factor-regression", "port": port, "entrypoint": "app.py"})
    demo.process = FakeProcess()
    monkey["cpu"] = cpu
    monkey["connections"] = connections
    return demo


class PathClassificationTests(unittest.TestCase):
    """Which requests are allowed to start a stopped demo."""

    def test_websocket_stream_is_reconnect_only(self):
        self.assertTrue(
            launcher.is_reconnect_only("/demos/factor-regression/_stcore/stream")
        )

    def test_health_and_host_config_are_reconnect_only(self):
        # Health has to count too: refusing the websocket while health still
        # answers 200 puts Streamlit's frontend into an undelayed retry loop.
        self.assertTrue(launcher.is_reconnect_only("/demos/factor-regression/_stcore/health"))
        self.assertTrue(
            launcher.is_reconnect_only("/demos/factor-regression/_stcore/host-config")
        )

    def test_query_string_and_trailing_slash_are_ignored(self):
        self.assertTrue(
            launcher.is_reconnect_only("/demos/factor-regression/_stcore/stream?x=1")
        )
        self.assertTrue(
            launcher.is_reconnect_only("/demos/factor-regression/_stcore/health/")
        )

    def test_document_request_may_start_a_demo(self):
        self.assertFalse(launcher.is_reconnect_only("/demos/factor-regression/"))
        self.assertFalse(launcher.is_reconnect_only("/demos/factor-regression"))

    def test_static_assets_may_start_a_demo(self):
        # A visitor arriving on a cold demo fetches assets; those must still work.
        self.assertFalse(
            launcher.is_reconnect_only("/demos/factor-regression/static/js/index.js")
        )
        self.assertFalse(
            launcher.is_reconnect_only("/demos/factor-regression/_stcore/allowed-message-origins")
        )

    def test_missing_header_may_start_a_demo(self):
        """If Caddy ever stops sending the header, fail towards working demos."""
        self.assertFalse(launcher.is_reconnect_only(None))
        self.assertFalse(launcher.is_reconnect_only(""))


class ActivateSlugParsingTests(unittest.TestCase):
    def test_parses_plain_activate_path(self):
        self.assertEqual(launcher.parse_activate_slug("/activate/momentum-factor"), "momentum-factor")

    def test_strips_a_query_string(self):
        # forward_auth appends the original request's query string onto the
        # fixed uri Caddy is configured with, e.g. momentum-factor's api
        # subpath being called with ?tickers=...&start=...
        self.assertEqual(
            launcher.parse_activate_slug("/activate/momentum-factor?tickers=NPN.JO&start=2024-01-01"),
            "momentum-factor",
        )

    def test_rejects_the_wrong_shape(self):
        self.assertIsNone(launcher.parse_activate_slug("/activate"))
        self.assertIsNone(launcher.parse_activate_slug("/activate/a/b"))
        self.assertIsNone(launcher.parse_activate_slug("/nope/momentum-factor"))


class CpuParsingTests(unittest.TestCase):
    def test_parses_utime_and_stime(self):
        # Fields after the name: state ppid pgrp session tty tpgid flags minflt
        # cminflt majflt cmajflt utime stime -> utime is index 11, stime 12.
        fields = ["S", "1", "1", "1", "0", "-1", "4194304", "100", "0", "0", "0", "150", "50"]
        line = f"4242 (streamlit) {' '.join(fields)}"

        self.assertAlmostEqual(launcher.parse_cpu_seconds(line, clock_ticks=100), 2.0)

    def test_tolerates_spaces_in_the_process_name(self):
        """The comm field is parenthesised and may contain spaces."""
        fields = ["S", "1", "1", "1", "0", "-1", "0", "0", "0", "0", "0", "300", "0"]
        line = f"4242 (streamlit run app) {' '.join(fields)}"

        self.assertAlmostEqual(launcher.parse_cpu_seconds(line, clock_ticks=100), 3.0)

    def test_missing_process_returns_none(self):
        self.assertIsNone(launcher.process_cpu_seconds(999999999))


class ReapDecisionTests(unittest.TestCase):
    """The decision table: when does a demo get stopped."""

    def setUp(self):
        self.state = {"cpu": 0.0, "connections": 0}
        self.addCleanup(self._restore, launcher.process_cpu_seconds, launcher.established_connections)
        launcher.process_cpu_seconds = lambda pid: self.state["cpu"]
        launcher.established_connections = lambda port: self.state["connections"]

    def _restore(self, cpu_fn, conn_fn):
        launcher.process_cpu_seconds = cpu_fn
        launcher.established_connections = conn_fn

    def _demo(self):
        demo = launcher.Demo(
            {"slug": "factor-regression", "port": 8502, "entrypoint": "app.py"}
        )
        demo.process = FakeProcess()
        return demo

    def test_idle_with_nobody_connected_is_stopped(self):
        demo = self._demo()
        demo.last_request = launcher.time.monotonic() - 600

        demo.stop_if_idle(idle_timeout=300, abandon_timeout=3600)

        self.assertIsNone(demo.process)

    def test_recently_used_with_nobody_connected_survives(self):
        demo = self._demo()
        demo.last_request = launcher.time.monotonic() - 10

        demo.stop_if_idle(idle_timeout=300, abandon_timeout=3600)

        self.assertIsNotNone(demo.process)

    def test_connected_and_burning_cpu_survives_past_the_abandon_timeout(self):
        """An actively used session must never be reaped, however long it runs."""
        demo = self._demo()
        demo.last_activity = launcher.time.monotonic() - 7200
        demo.last_cpu = 0.0
        self.state["connections"] = 1
        self.state["cpu"] = 5.0  # a rerun happened since the last check

        demo.stop_if_idle(idle_timeout=300, abandon_timeout=3600, cpu_active_delta=0.2)

        self.assertIsNotNone(demo.process, "an active session was reaped")

    def test_connected_but_no_cpu_activity_is_stopped(self):
        """The abandoned tab: attached, but the process has done nothing."""
        demo = self._demo()
        demo.last_activity = launcher.time.monotonic() - 7200
        demo.last_cpu = 5.0
        self.state["connections"] = 1
        self.state["cpu"] = 5.0001  # below the activity threshold

        demo.stop_if_idle(idle_timeout=300, abandon_timeout=3600, cpu_active_delta=0.2)

        self.assertIsNone(demo.process)

    def test_connected_and_idle_but_within_the_abandon_timeout_survives(self):
        demo = self._demo()
        demo.last_activity = launcher.time.monotonic() - 60
        self.state["connections"] = 1
        self.state["cpu"] = 0.0

        demo.stop_if_idle(idle_timeout=300, abandon_timeout=3600)

        self.assertIsNotNone(demo.process)

    def test_connection_defers_the_plain_idle_rule(self):
        """A long-lived session sends no requests, so last_request must not kill it."""
        demo = self._demo()
        demo.last_request = launcher.time.monotonic() - 7200
        demo.last_activity = launcher.time.monotonic()
        self.state["connections"] = 1

        demo.stop_if_idle(idle_timeout=300, abandon_timeout=3600)

        self.assertIsNotNone(demo.process)

    def test_abandon_timeout_of_zero_disables_the_abandoned_rule(self):
        demo = self._demo()
        demo.last_activity = launcher.time.monotonic() - 7200
        self.state["connections"] = 1

        demo.stop_if_idle(idle_timeout=300, abandon_timeout=0)

        self.assertIsNotNone(demo.process)

    def test_already_stopped_demo_is_left_alone(self):
        demo = self._demo()
        demo.process = None

        demo.stop_if_idle(idle_timeout=0, abandon_timeout=0)

        self.assertIsNone(demo.process)


class StartPolicyTests(unittest.TestCase):
    """ensure_running must not start anything when may_start is False."""

    def setUp(self):
        self.addCleanup(self._restore, launcher.port_is_listening)
        launcher.port_is_listening = lambda port, timeout=0.5: False

    def _restore(self, fn):
        launcher.port_is_listening = fn

    def test_reconnect_does_not_start_a_stopped_demo(self):
        demo = launcher.Demo(
            {"slug": "factor-regression", "port": 8502, "entrypoint": "app.py"}
        )

        self.assertFalse(demo.ensure_running(may_start=False))
        self.assertIsNone(demo.process, "a reconnect resurrected a reaped demo")


class BuildCommandTests(unittest.TestCase):
    def test_file_watcher_is_disabled(self):
        command = launcher.build_command(
            {"slug": "factor-regression", "port": 8502, "entrypoint": "app/streamlit_app.py"}
        )

        self.assertIn("--server.fileWatcherType=none", command)

    def test_base_url_path_matches_the_slug(self):
        command = launcher.build_command(
            {"slug": "factor-regression", "port": 8502, "entrypoint": "app/streamlit_app.py"}
        )

        self.assertIn("--server.baseUrlPath=/demos/factor-regression", command)
        self.assertIn("--server.port=8502", command)

    def test_api_kind_runs_python_directly_with_a_port_flag(self):
        command = launcher.build_command(
            {"slug": "momentum-factor", "kind": "api", "port": 8501, "entrypoint": "app/api.py"}
        )

        self.assertEqual(
            command,
            ["/app/.venv/bin/python", "/app/apps/momentum-factor/app/api.py", "--port=8501"],
        )


if __name__ == "__main__":
    unittest.main()
