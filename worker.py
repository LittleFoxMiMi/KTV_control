import sys
import os
import subprocess
import time


def start_consumer(queue_name: str):
    cmd = [
        sys.executable,
        "-m",
        "huey.bin.huey_consumer",
        "src.tasks.huey",
        "-q",
        queue_name,
        "-w",
        "1"
    ]
    return subprocess.Popen(cmd, cwd=os.getcwd())


if __name__ == "__main__":
    print("Starting Huey Workers (parse/download/separate)...")
    processes = [
        start_consumer("parse"),
        start_consumer("download"),
        start_consumer("separate")
    ]

    try:
        while True:
            time.sleep(1)
            # If any worker exits, stop all
            for p in processes:
                if p.poll() is not None:
                    raise RuntimeError("A worker exited unexpectedly.")
    except KeyboardInterrupt:
        pass
    finally:
        for p in processes:
            try:
                p.terminate()
            except Exception:
                pass
