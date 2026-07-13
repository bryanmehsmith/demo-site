"""Launches every Streamlit demo listed in demos.json on its own internal port."""
import json
import pathlib
import subprocess

DEMOS = json.loads(pathlib.Path("/app/demos.json").read_text())


def main():
    procs = []
    for demo in DEMOS:
        if demo["kind"] != "streamlit":
            continue
        streamlit_bin = f"/app/apps/{demo['slug']}/.venv/bin/streamlit"
        entrypoint = f"/app/apps/{demo['slug']}/{demo['entrypoint']}"
        cmd = [
            streamlit_bin, "run", entrypoint,
            f"--server.port={demo['port']}",
            "--server.address=127.0.0.1",
            f"--server.baseUrlPath=/demos/{demo['slug']}",
            "--server.headless=true",
        ]
        procs.append(subprocess.Popen(cmd))

    for proc in procs:
        proc.wait()


if __name__ == "__main__":
    main()
