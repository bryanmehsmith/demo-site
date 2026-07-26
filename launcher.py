"""Starts each Streamlit demo on first request and stops it once nobody is using it.

Demos used to all start at container boot, so every demo cost its memory whether
or not anyone visited. With one demo that was invisible; with several it does not
scale, since each analysis demo holds a few hundred MiB of the scientific Python
stack.

Caddy calls this service via `forward_auth` before proxying a demo request. The
handler makes sure the demo is running, waits for its port to accept
connections, and only then returns 200, at which point Caddy proxies as usual and
handles the websocket itself. The cost is a few seconds on the first request to a
cold demo; every later request is a dictionary lookup.

Because Caddy asks about every request, this is also where traffic is observed, so
a reaper thread can stop demos that have gone idle.
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

DEMOS = {
    demo["slug"]: demo
    for demo in json.loads(pathlib.Path("/app/demos.json").read_text())
    if demo["kind"] == "streamlit"
}

LISTEN_PORT = 9000
# How long to wait for a cold demo to start listening before giving up.
STARTUP_TIMEOUT = float(os.environ.get("DEMO_STARTUP_TIMEOUT", "90"))
# Stop a demo after this long with no requests and no connected clients.
# 0 disables reaping, which is useful when debugging.
#
# Matched to the container app's own scale-to-zero cooldown (300s). The replica
# disappears entirely after 5 minutes without traffic, taking every demo with it,
# so a longer idle timeout than that would almost never get the chance to fire.
# Keeping them equal means the reaper still earns its place in the case that does
# matter: a visitor reading one demo while another sits idle, where traffic keeps
# the replica alive but the idle demo's memory should still come back.
IDLE_TIMEOUT = float(os.environ.get("DEMO_IDLE_TIMEOUT", "300"))
REAPER_INTERVAL = float(os.environ.get("DEMO_REAPER_INTERVAL", "30"))

_TCP_ESTABLISHED = "01"


def log(message: str) -> None:
    print(f"[launcher] {message}", flush=True)


def build_command(demo: dict) -> list[str]:
    slug = demo["slug"]
    return [
        f"/app/apps/{slug}/.venv/bin/streamlit", "run",
        f"/app/apps/{slug}/{demo['entrypoint']}",
        f"--server.port={demo['port']}",
        "--server.address=127.0.0.1",
        f"--server.baseUrlPath=/demos/{slug}",
        "--server.headless=true",
    ]


def port_is_listening(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def established_connections(port: int) -> int:
    """Count clients currently connected to a demo's port.

    A websocket session shows up here as a single long-lived connection but
    generates no further HTTP requests, so without this check the reaper would
    happily stop a demo out from under someone mid-session.
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


class Demo:
    """One demo's process, started on demand and stopped when idle."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.slug = spec["slug"]
        self.port = spec["port"]
        self.process: subprocess.Popen | None = None
        self.last_request = 0.0
        self.lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def ensure_running(self) -> bool:
        """Start the demo if needed and return True once its port accepts connections."""
        self.last_request = time.monotonic()

        with self.lock:
            if self.running and port_is_listening(self.port):
                return True

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
                    self.last_request = time.monotonic()
                    return True
                time.sleep(0.25)

            log(f"{self.slug} did not start within {STARTUP_TIMEOUT}s")
            return False

    def stop_if_idle(self, idle_timeout: float) -> None:
        with self.lock:
            if not self.running:
                return
            clients = established_connections(self.port)
            if clients:
                # Someone is connected, so treat this as activity.
                self.last_request = time.monotonic()
                return
            idle_for = time.monotonic() - self.last_request
            if idle_for < idle_timeout:
                return

            log(f"stopping {self.slug} after {idle_for:.0f}s idle")
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log(f"{self.slug} ignored SIGTERM, killing")
                self.process.kill()
                self.process.wait(timeout=10)
            self.process = None


DEMO_STATE = {slug: Demo(spec) for slug, spec in DEMOS.items()}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        # Caddy is configured to call /activate/<slug>.
        parts = self.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "activate":
            self._respond(404, "expected /activate/<slug>\n")
            return

        slug = parts[1]
        demo = DEMO_STATE.get(slug)
        if demo is None:
            self._respond(404, f"unknown demo {slug!r}\n")
            return

        if demo.ensure_running():
            self._respond(200, "ready\n")
        else:
            self._respond(503, f"{slug} failed to start\n")

    # Same handling for the HEAD and POST that Caddy may mirror.
    do_HEAD = do_GET
    do_POST = do_GET

    def log_message(self, format: str, *args) -> None:
        # Silence per-request noise; the interesting transitions are logged above.
        pass


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not shout about ordinary disconnects.

    Caddy keeps connections to this service alive and reuses them. When it drops
    one, the handler thread waiting on the next request line sees ECONNRESET, and
    socketserver's default behaviour is to dump a full traceback to stderr. That
    filled the container logs with alarming but entirely harmless
    ConnectionResetError stacks, so those two cases are swallowed and anything
    genuinely unexpected is still reported.
    """

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def reaper() -> None:
    if IDLE_TIMEOUT <= 0:
        log("idle reaping disabled")
        return
    while True:
        time.sleep(REAPER_INTERVAL)
        for demo in DEMO_STATE.values():
            try:
                demo.stop_if_idle(IDLE_TIMEOUT)
            except Exception as exc:
                log(f"reaper error for {demo.slug}: {type(exc).__name__}: {exc}")


def main() -> None:
    log(f"managing demos: {', '.join(sorted(DEMOS)) or 'none'}")
    log(f"idle timeout {IDLE_TIMEOUT:.0f}s, startup timeout {STARTUP_TIMEOUT:.0f}s")
    log("no demo is started until a request arrives for it")

    threading.Thread(target=reaper, daemon=True).start()
    Server(("127.0.0.1", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
