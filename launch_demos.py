"""Launches every Streamlit demo listed in demos.json on its own internal port.

Each demo is supervised: if its process dies, it is restarted. Without this, one
demo crashing (an OOM kill, an unhandled exception) took that demo down until the
next deploy, while Caddy stayed up and kept serving the other demos. The
container therefore looked healthy to Azure and was never restarted, so the dead
demo returned 502 indefinitely.
"""
import json
import pathlib
import subprocess
import time

DEMOS = json.loads(pathlib.Path("/app/demos.json").read_text())

POLL_SECONDS = 5
# Back off on a crash-looping demo so a permanently broken one does not spin.
MIN_RESTART_BACKOFF = 5
MAX_RESTART_BACKOFF = 300
# Stay up this long and the previous crash is treated as a one-off, so the next
# one is retried promptly again. Without this the backoff only ever grows, and a
# demo that crashed twice months apart would sit behind a 5 minute delay.
HEALTHY_RESET_SECONDS = 60


def build_command(demo: dict) -> list[str]:
    streamlit_bin = f"/app/apps/{demo['slug']}/.venv/bin/streamlit"
    entrypoint = f"/app/apps/{demo['slug']}/{demo['entrypoint']}"
    return [
        streamlit_bin, "run", entrypoint,
        f"--server.port={demo['port']}",
        "--server.address=127.0.0.1",
        f"--server.baseUrlPath=/demos/{demo['slug']}",
        "--server.headless=true",
    ]


def main():
    streamlit_demos = [demo for demo in DEMOS if demo["kind"] == "streamlit"]

    supervised = {}
    for demo in streamlit_demos:
        supervised[demo["slug"]] = {
            "demo": demo,
            "process": subprocess.Popen(build_command(demo)),
            "backoff": MIN_RESTART_BACKOFF,
            "restart_at": None,
            "started_at": time.monotonic(),
        }
        print(f"started {demo['slug']} on port {demo['port']}", flush=True)

    while supervised:
        time.sleep(POLL_SECONDS)
        now = time.monotonic()

        for slug, state in supervised.items():
            process = state["process"]

            if process is not None:
                if process.poll() is None:
                    if (
                        state["backoff"] > MIN_RESTART_BACKOFF
                        and now - state["started_at"] >= HEALTHY_RESET_SECONDS
                    ):
                        state["backoff"] = MIN_RESTART_BACKOFF
                        print(f"{slug} is stable again; restart delay reset", flush=True)
                    continue

                print(
                    f"{slug} exited with code {process.returncode}; "
                    f"restarting in {state['backoff']}s",
                    flush=True,
                )
                state["process"] = None
                state["restart_at"] = now + state["backoff"]
                continue

            if now < state["restart_at"]:
                continue

            state["process"] = subprocess.Popen(build_command(state["demo"]))
            state["restart_at"] = None
            state["started_at"] = now
            # Widen the backoff in case this restart fails too. It is reset above
            # once the demo has stayed up for HEALTHY_RESET_SECONDS.
            state["backoff"] = min(state["backoff"] * 2, MAX_RESTART_BACKOFF)
            print(f"restarted {slug} on port {state['demo']['port']}", flush=True)


if __name__ == "__main__":
    main()
