import sys
import os
import subprocess
import time
import glob


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


def cleanup_lock_files():
    lock_dir = os.path.join("Temp", "locks")
    if not os.path.isdir(lock_dir):
        return

    removed = 0
    skipped = 0
    for lock_path in glob.glob(os.path.join(lock_dir, "*.lock")):
        try:
            os.remove(lock_path)
            removed += 1
        except Exception:
            skipped += 1

    print(f"Lock cleanup: removed={removed}, skipped={skipped}")


if __name__ == "__main__":
    print("Starting Huey Workers (parse/download/separate/music_serial)...")
    cleanup_lock_files()
    processes = [
        start_consumer("parse"),
        start_consumer("download"),
        start_consumer("separate"),
        start_consumer("music_serial")
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
