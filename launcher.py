"""Starts optional demo backends on first request and stops them when idle.

Every current demo has a static frontend. Momentum-factor and factor-regression
each have a small JSON API (kind "api" in demos.json) used only for optional
live-data requests. This launcher starts those companion processes on demand.
It also retains the generic "streamlit" kind for future process-backed demos.

Process-backed demos used to start at container boot, so each one consumed
memory whether or not anyone used its optional backend. Starting them on demand
keeps that scientific Python stack out of memory until it is needed.

Caddy calls this service via `forward_auth` before proxying a demo request. The
handler makes sure the demo is running, waits for its port to accept
connections, and only then returns 200, at which point Caddy proxies as usual and
handles the websocket itself. The cost is a few seconds on the first request to a
cold demo; every later request is a dictionary lookup.

Two kinds of idleness are reaped, because they need different evidence:

* Nobody connected, no requests for `IDLE_TIMEOUT`. Straightforward.
* Somebody still connected, but the process has burned no CPU for
  `ABANDON_TIMEOUT`. This is the browser tab left open for days. A websocket
  session sends no further HTTP requests, so request timestamps cannot tell an
  abandoned tab from a busy one; CPU time can, because a rerun costs a
  measurable slice and sitting still costs essentially nothing.

For the retained Streamlit mode, requests that only a reconnecting client makes
are answered without starting anything. Otherwise, a reaped session could be
immediately resurrected by an abandoned tab's retry loop.
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEMOS_PATH = pathlib.Path(os.environ.get("DEMOS_JSON", "/app/demos.json"))

LISTEN_PORT = 9000
# How long to wait for a cold demo to start listening before giving up.
STARTUP_TIMEOUT = float(os.environ.get("DEMO_STARTUP_TIMEOUT", "90"))
# Stop a demo after this long with no requests and nobody connected.
# 0 disables reaping, which is useful when debugging.
#
# Matched to the container app's own scale-to-zero cooldown (300s). The replica
# disappears entirely after 5 minutes without traffic, taking every demo with it,
# so a longer idle timeout than that would almost never get the chance to fire.
IDLE_TIMEOUT = float(os.environ.get("DEMO_IDLE_TIMEOUT", "300"))
# Stop a demo after this long with someone still connected but no CPU activity.
# This is the abandoned-tab case, so it is deliberately generous: a visitor
# reading a demo for this long without touching a single widget gets
# disconnected and has to reload.
ABANDON_TIMEOUT = float(os.environ.get("DEMO_ABANDON_TIMEOUT", "3600"))
REAPER_INTERVAL = float(os.environ.get("DEMO_REAPER_INTERVAL", "30"))
# CPU seconds within one reaper interval that count as somebody doing something.
#
# Measured in the container at 0.5 CPU: a connected but idle session burns about
# 0.11s per 30s, and that floor rises with the number of idle sessions, while a
# single rerun of the factor-regression demo costs several CPU-seconds. 0.5 sits
# well clear of the idle floor and well below one rerun, so neither a handful of
# idle tabs reads as activity nor does a real interaction read as idleness.
CPU_ACTIVE_DELTA = float(os.environ.get("DEMO_CPU_ACTIVE_DELTA", "0.5"))

_TCP_ESTABLISHED = "01"
_CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

# Paths only ever requested by a client that is already trying to reconnect.
# `health` and `host-config` are what Streamlit polls while disconnected, and
# `stream` is the websocket itself. A first page load asks for the document and
# its assets, never these alone.
RECONNECT_ONLY_SUFFIXES = ("/_stcore/stream", "/_stcore/health", "/_stcore/host-config")


def log(message: str) -> None:
    print(f"[launcher] {message}", flush=True)


# Kinds this launcher knows how to start a process for. "streamlit" is a full
# Streamlit app; "api" is a small stdlib-only JSON backend (e.g. momentum-factor's
# live-refresh endpoint), sitting behind a static frontend that needs no process
# of its own the rest of the time.
KNOWN_KINDS = ("streamlit", "api")


def load_demos(path: pathlib.Path = DEMOS_PATH) -> dict[str, dict]:
    return {
        demo["slug"]: demo
        for demo in json.loads(path.read_text())
        if demo["kind"] in KNOWN_KINDS
    }


# All demos share one uv workspace venv (see Dockerfile), so every process is
# launched with the same interpreter regardless of which app it belongs to.
SHARED_VENV_PYTHON = "/app/.venv/bin/python"


def build_command(demo: dict) -> list[str]:
    slug = demo["slug"]
    if demo.get("kind") == "api":
        return [
            SHARED_VENV_PYTHON,
            f"/app/apps/{slug}/{demo['entrypoint']}",
            f"--port={demo['port']}",
        ]
    return [
        SHARED_VENV_PYTHON, "-m", "streamlit", "run",
        f"/app/apps/{slug}/{demo['entrypoint']}",
        f"--server.port={demo['port']}",
        "--server.address=127.0.0.1",
        f"--server.baseUrlPath=/demos/{slug}",
        "--server.headless=true",
        # Nothing can edit the source inside an immutable image, and the watcher
        # falls back to polling here, which burns CPU forever for no reason. It
        # also muddies the CPU signal the abandoned-tab reaper depends on.
        "--server.fileWatcherType=none",
    ]


def parse_activate_slug(path: str) -> str | None:
    """Extract <slug> from a launcher request path of the form /activate/<slug>.

    forward_auth appends the original request's own query string onto the
    fixed `uri` given in the Caddyfile (e.g. momentum-factor's api subpath is
    called with ?tickers=...), so that must be stripped before parsing rather
    than assumed absent.
    """
    path = path.split("?", 1)[0]
    parts = path.strip("/").split("/")
    if len(parts) != 2 or parts[0] != "activate":
        return None
    return parts[1]


def is_reconnect_only(original_uri: str | None) -> bool:
    """Would only a reconnecting client ask for this?

    Used to decide whether a request is allowed to start a stopped demo. Query
    strings are stripped first because Streamlit appends them to the websocket
    URL.
    """
    if not original_uri:
        return False
    path = original_uri.split("?", 1)[0].rstrip("/")
    return path.endswith(RECONNECT_ONLY_SUFFIXES)


def port_is_listening(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def established_connections(port: int) -> int:
    """Count clients currently connected to a demo's port.

    A websocket session shows up here as a single long-lived connection but
    generates no further HTTP requests, so this is the only way to know whether
    anybody is attached.
    """
    wanted = f"{port:04X}"
    count = 0
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = pathlib.Path(name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4:
                continue
            local_port = fields[1].rsplit(":", 1)[-1]
            if local_port == wanted and fields[3] == _TCP_ESTABLISHED:
                count += 1
    return count


def parse_cpu_seconds(stat_line: str, clock_ticks: int = _CLOCK_TICKS) -> float:
    """CPU seconds (user + system) out of a /proc/<pid>/stat line.

    The process name sits in parentheses and can itself contain spaces, so the
    fields are counted from the closing parenthesis rather than by splitting the
    whole line. utime and stime are fields 14 and 15 one-based, which is index 11
    and 12 counting from the field after the name.
    """
    _, _, rest = stat_line.partition(") ")
    fields = rest.split()
    utime, stime = int(fields[11]), int(fields[12])
    return (utime + stime) / clock_ticks


def process_cpu_seconds(pid: int) -> float | None:
    try:
        return parse_cpu_seconds(pathlib.Path(f"/proc/{pid}/stat").read_text())
    except (OSError, IndexError, ValueError):
        return None


class Demo:
    """One demo's process, started on demand and stopped when idle or abandoned."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.slug = spec["slug"]
        self.port = spec["port"]
        self.process: subprocess.Popen | None = None
        self.last_request = 0.0
        # Last time the process was observed doing work, and the CPU reading it
        # was judged against.
        self.last_activity = 0.0
        self.last_cpu = 0.0
        self.lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _mark_started(self) -> None:
        now = time.monotonic()
        self.last_request = now
        self.last_activity = now
        self.last_cpu = process_cpu_seconds(self.process.pid) or 0.0

    def ensure_running(self, may_start: bool = True) -> bool:
        """Return True once the demo's port accepts connections.

        With `may_start` False the demo is never started, only reported on. That
        is how a reconnecting client is stopped from resurrecting a demo that was
        deliberately reaped.
        """
        self.last_request = time.monotonic()

        with self.lock:
            if self.running and port_is_listening(self.port):
                return True

            if not may_start:
                return False

            if self.process is not None and not self.running:
                log(f"{self.slug} had died (exit {self.process.returncode}), restarting")
                self.process = None

            if self.process is None:
                log(f"starting {self.slug} on port {self.port}")
                self.process = subprocess.Popen(build_command(self.spec))

            deadline = time.monotonic() + STARTUP_TIMEOUT
            while time.monotonic() < deadline:
                if not self.running:
                    log(f"{self.slug} exited during startup (exit {self.process.returncode})")
                    self.process = None
                    return False
                if port_is_listening(self.port):
                    log(f"{self.slug} is ready")
                    self._mark_started()
                    return True
                time.sleep(0.25)

            log(f"{self.slug} did not start within {STARTUP_TIMEOUT}s")
            return False

    def _stop(self, reason: str) -> None:
        log(f"stopping {self.slug}: {reason}")
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log(f"{self.slug} ignored SIGTERM, killing")
            self.process.kill()
            self.process.wait(timeout=10)
        self.process = None

    def stop_if_idle(
        self,
        idle_timeout: float = IDLE_TIMEOUT,
        abandon_timeout: float = ABANDON_TIMEOUT,
        cpu_active_delta: float = CPU_ACTIVE_DELTA,
    ) -> None:
        with self.lock:
            if not self.running:
                return

            now = time.monotonic()
            cpu = process_cpu_seconds(self.process.pid)
            if cpu is not None:
                if cpu - self.last_cpu >= cpu_active_delta:
                    self.last_activity = now
                self.last_cpu = cpu

            if established_connections(self.port):
                # Somebody is attached, so the plain idle rule does not apply and
                # only a total absence of work counts as abandonment.
                self.last_request = now
                if abandon_timeout <= 0:
                    return
                abandoned_for = now - self.last_activity
                if abandoned_for >= abandon_timeout:
                    self._stop(
                        f"connected but no activity for {abandoned_for / 60:.0f}m "
                        "(abandoned tab)"
                    )
                return

            if idle_timeout <= 0:
                return
            idle_for = now - self.last_request
            if idle_for >= idle_timeout:
                self._stop(f"idle for {idle_for:.0f}s with nobody connected")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Set by main() so the handler can reach the demo registry.
    demos: dict[str, Demo] = {}

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        # Caddy is configured to call /activate/<slug> and to pass the request it
        # is really handling in X-Original-Uri.
        slug = parse_activate_slug(self.path)
        if slug is None:
            self._respond(404, "expected /activate/<slug>\n")
            return
        demo = self.demos.get(slug)
        if demo is None:
            self._respond(404, f"unknown demo {slug!r}\n")
            return

        original_uri = self.headers.get("X-Original-Uri")
        may_start = not is_reconnect_only(original_uri)

        if demo.ensure_running(may_start=may_start):
            self._respond(200, "ready\n")
        elif may_start:
            self._respond(503, f"{slug} failed to start\n")
        else:
            # Deliberately fails the health poll too. Refusing the websocket
            # while health still answered 200 puts Streamlit's frontend into an
            # undelayed retry loop; failing both holds it at its two second
            # ceiling instead.
            self._respond(503, f"{slug} is stopped; reload the page to start it\n")

    # Same handling for the HEAD and POST that Caddy may mirror.
    do_HEAD = do_GET
    do_POST = do_GET

    def log_message(self, format: str, *args) -> None:
        # Silence per-request noise; the interesting transitions are logged above.
        pass


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not shout about ordinary disconnects.

    Caddy keeps its connections to this service alive and reuses them. When it
    drops one, the handler thread waiting on the next request line sees
    ECONNRESET, and socketserver's default behaviour is to dump a full traceback
    to stderr. That filled the container logs with alarming but entirely harmless
    ConnectionResetError stacks, so those cases are swallowed and anything
    genuinely unexpected is still reported.
    """

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def reaper(demos: dict[str, Demo]) -> None:
    if IDLE_TIMEOUT <= 0 and ABANDON_TIMEOUT <= 0:
        log("reaping disabled")
        return
    while True:
        time.sleep(REAPER_INTERVAL)
        for demo in demos.values():
            try:
                demo.stop_if_idle()
            except Exception as exc:
                log(f"reaper error for {demo.slug}: {type(exc).__name__}: {exc}")


def main() -> None:
    specs = load_demos()
    demos = {slug: Demo(spec) for slug, spec in specs.items()}
    Handler.demos = demos

    log(f"managing demos: {', '.join(sorted(specs)) or 'none'}")
    log(
        f"idle timeout {IDLE_TIMEOUT:.0f}s, abandoned timeout {ABANDON_TIMEOUT:.0f}s, "
        f"startup timeout {STARTUP_TIMEOUT:.0f}s"
    )
    log("no demo is started until a request arrives for it")

    threading.Thread(target=reaper, args=(demos,), daemon=True).start()
    Server(("127.0.0.1", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
