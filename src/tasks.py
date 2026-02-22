import os
import json
import subprocess
import re
import traceback
import time
from contextlib import contextmanager
from huey import SqliteHuey
from huey.api import Task
from src.database import SongDatabase

# Config Loading
CONFIG_PATH = "config.json"
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
else:
    # Fallback default
    config = {
        "paths": {
            "songs_dir": "./songs",
            "vocal_remover_script": "./src/vocal_remover.py",
            "python_path": "python",
            "yt_dlp_path": "yt-dlp"
        },
        "processing": {"gpu_id": 0}
    }

# Tasks Setup
huey = SqliteHuey(filename='tasks.db')
db = SongDatabase()

QUEUE_PARSE = 'parse'
QUEUE_DOWNLOAD = 'download'
QUEUE_SEPARATE = 'separate'

@contextmanager
def _task_lock(lock_name: str):
    lock_dir = os.path.join("Temp", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, f"{lock_name}.lock")

    lock_file = open(lock_path, 'a+')
    try:
        if os.name == 'nt':
            import msvcrt
            while True:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        yield
    finally:
        try:
            if os.name == 'nt':
                import msvcrt
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

def run_command_with_progress(cmd, song_id, progress_parser=None, status_prefix=""):
    """
    Runs a subprocess commands and updates DB status/progress
    """
    print(f"[Task] Executing: {' '.join(cmd)}")
    
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors='replace' 
    )

    while True:
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            break
        if line:
            line = line.strip()
            
            if progress_parser:
                progress = progress_parser(line)
                if progress is not None:
                    db.update_status(song_id, "downloading" if "download" in status_prefix.lower() else "processing", progress, status_detail=status_prefix)
    
    return process.poll()

def _get_song(song_id: int):
    try:
        return db.get_song_by_id(song_id)
    except AttributeError:
        return db.get_song(song_id)




@huey.task(queue=QUEUE_PARSE)
def process_song_task(song_id: int):
    song = _get_song(song_id)
    if not song:
        return

    if song['status'] == 'completed':
        return

    songs_dir = config['paths']['songs_dir']
    os.makedirs(songs_dir, exist_ok=True)
    
    def dl_progress_parser(line):
        if "[download]" in line:
            m = re.search(r'(\d+\.?\d*)%', line)
            if m:
                return float(m.group(1))
        return None

    db.update_status(song_id, "downloading", 0, status_detail="Resolving Info...")
    
    yt_dlp_path = config['paths'].get('yt_dlp_path', 'yt-dlp')
    
    # 1. Resolve Video ID if missing
    video_id = song.get('video_id')
    video_title = song.get('title')
    
    if not video_id:
        print(f"Resolving Video ID for {song['url']}...")
        try:
            cookies_path = os.path.join("cookies", "cookies.txt")
            cookies_arg = ['--cookies', cookies_path] if os.path.exists(cookies_path) else []
            res = subprocess.run(
                [yt_dlp_path, song['url'], '-j', '--playlist-items', '1', '--no-playlist'] + cookies_arg,
                capture_output=True, text=True, encoding='utf-8', errors='ignore'
            )
            if res.returncode == 0 and res.stdout:
                meta = json.loads(res.stdout.split('\n')[0])
                video_id = meta.get('id')
                video_title = meta.get('title')
                 
                 # Bilibili ID Consistency Fix: 
                 # Short links resolve to 'BV..._p1', Long links to 'BV...'.
                 # Enforce '_p1' suffix if missing so they match in Library.
                if video_id and video_id.startswith('BV') and not re.search(r'_p\d+$', video_id):
                    video_id += '_p1'
                 
                 # Save to DB immediately
                db.update_paths(song_id, video_id=video_id, title=video_title)
            else:
                err_msg = (res.stderr or res.stdout or "(no output)").strip()
                print(f"Failed to resolve ID. ReturnCode={res.returncode}. Output: {err_msg}")
                db.update_status(song_id, "error", 0, error_msg="Resolve Failed", status_detail="Resolve Failed")
                return
        except Exception as e:
            print(f"Error resolving ID: {e}")
            db.update_status(song_id, "error", 0, error_msg="Resolve Failed", status_detail="Resolve Failed")
            return
            
    # Platform Detection
    if 'bilibili' in song['url'] or 'b23.tv' in song['url']:
        platform = 'bilibili'
    elif 'youtube' in song['url'] or 'youtu.be' in song['url']:
        platform = 'youtube'
    else:
        platform = 'other'

    # Check Library
    if video_id:
        lib_entry = db.get_library_song(video_id)
        if lib_entry:
            print(f"Found {video_id} in Library. Verifying files...")
            
            # Construct full paths from relative library paths
            full_video = os.path.join(songs_dir, lib_entry['video_path']) if lib_entry['video_path'] else None
            full_inst = os.path.join(songs_dir, lib_entry['audio_path']) if lib_entry['audio_path'] else None
            
            # We strictly require Video and Instrumental for a "Hit"
            if full_video and os.path.exists(full_video) and full_inst and os.path.exists(full_inst):
                 print(f"Library hit confirmed for {video_id}.")
                 db.update_paths(song_id, 
                                 video_id=video_id,
                                 title=lib_entry['title'] or video_title,
                                 video_path=lib_entry['video_path'], 
                                 audio_path=lib_entry['audio_path'], 
                                 raw_audio_path=lib_entry['raw_audio_path'])
                 db.update_status(song_id, "completed", 100, status_detail="Restored from Library")
                 
                 # Update 'last_used' in library
                 db.save_to_library(video_id, lib_entry['title'] or video_title, lib_entry['video_path'], lib_entry['audio_path'], lib_entry['raw_audio_path'], platform)
                 return

    download_song_task(song_id)


@huey.task(queue=QUEUE_DOWNLOAD)
def download_song_task(song_id: int):
    song = _get_song(song_id)
    if not song:
        return

    if song['status'] == 'completed':
        return

    songs_dir = config['paths']['songs_dir']
    os.makedirs(songs_dir, exist_ok=True)

    def dl_progress_parser(line):
        if "[download]" in line:
            m = re.search(r'(\d+\.?\d*)%', line)
            if m:
                return float(m.group(1))
        return None

    video_id = song.get('video_id')
    video_title = song.get('title')
    if not video_id:
        db.update_status(song_id, "error", 0, error_msg="Missing video id", status_detail="Missing video id")
        return

    # Platform Detection
    if 'bilibili' in song['url'] or 'b23.tv' in song['url']:
        platform = 'bilibili'
    elif 'youtube' in song['url'] or 'youtu.be' in song['url']:
        platform = 'youtube'
    else:
        platform = 'other'

    with _task_lock("download"):
        try:
            target_dir = os.path.join(songs_dir, platform, video_id)
            os.makedirs(target_dir, exist_ok=True)

            # Check if files already exist
            should_download = True
            if os.path.exists(target_dir):
                existing_files = os.listdir(target_dir)
                has_video = any(f.startswith(video_id) and not 'instrumental' in f and f.lower().endswith(('.mp4','.webm','.mkv')) for f in existing_files)
                has_inst = any('instrumental' in f for f in existing_files)

                if has_video and has_inst:
                    print(f"Files found for {video_id}, skipping download.")
                    should_download = False

                    candidates = [os.path.join(target_dir, f) for f in existing_files if f.startswith(video_id) and not 'instrumental' in f]
                    video_path = candidates[0] if candidates else None

                    inst_files = [os.path.join(target_dir, f) for f in existing_files if 'instrumental' in f]
                    audio_inst = inst_files[0] if inst_files else None

                    audio_raw = video_path

                    rel_v = os.path.relpath(video_path, songs_dir)
                    rel_i = os.path.relpath(audio_inst, songs_dir)
                    rel_r = os.path.relpath(audio_raw, songs_dir)

                    db.update_paths(song_id, video_path=rel_v, audio_path=rel_i, raw_audio_path=rel_r)
                    db.update_status(song_id, "completed", 100, status_detail="Ready")
                    return

            if should_download:
                db.update_status(song_id, "downloading", 0, status_detail="Downloading")

                cookies_path = os.path.join("cookies", "cookies.txt")
                cookies_arg = ['--cookies', cookies_path] if os.path.exists(cookies_path) else []

                out_tmpl = os.path.join(target_dir, f"{video_id}.%(ext)s")
                cmd = [
                    config['paths'].get('yt_dlp_path', 'yt-dlp'),
                    song['url'],
                    '-f', 'bv[ext=mp4],ba[ext=m4a]',
                    '-o', out_tmpl,
                    '--no-playlist'
                ] + cookies_arg

                ret = run_command_with_progress(cmd, song_id, dl_progress_parser, status_prefix="Downloading")
                if ret != 0:
                    db.update_status(song_id, "error", 0, error_msg="Download Failed", status_detail="Download Failed")
                    return

                video_files = [f for f in os.listdir(target_dir) if f.startswith(video_id) and f.endswith('.mp4')]
                audio_files = [f for f in os.listdir(target_dir) if f.startswith(video_id) and f.endswith('.m4a')]

                if not video_files:
                    db.update_status(song_id, "error", error_msg="Video file missing", status_detail="Video file missing")
                    return

                video_filename = video_files[0]
                full_video_path = os.path.join(target_dir, video_filename)

                if audio_files:
                    audio_raw_filename = audio_files[0]
                    full_audio_raw_path = os.path.join(target_dir, audio_raw_filename)
                else:
                    print("Warning: .m4a file not found, using video file for raw audio")
                    full_audio_raw_path = full_video_path

                rel_video_path = os.path.relpath(full_video_path, songs_dir)
                rel_raw_audio_path = os.path.relpath(full_audio_raw_path, songs_dir)

                db.update_paths(song_id, video_path=rel_video_path, raw_audio_path=rel_raw_audio_path)

            separate_song_task(song_id)

        except Exception as e:
            print(f"Task Error: {e}")
            traceback.print_exc()
            db.update_status(song_id, "error", error_msg=str(e))


@huey.task(queue=QUEUE_SEPARATE)
def separate_song_task(song_id: int):
    song = _get_song(song_id)
    if not song:
        return

    if song['status'] == 'completed':
        return

    songs_dir = config['paths']['songs_dir']
    os.makedirs(songs_dir, exist_ok=True)

    # Define progress parser for vocal_remover output "[PROGRESS] 50"
    def parse_progress(line):
        m = re.match(r'\[PROGRESS\] (\d+)', line)
        if m:
            return int(m.group(1))
        return None

    with _task_lock("separate"):
        try:
            video_id = song.get('video_id')
            video_title = song.get('title')

            # Platform Detection
            if 'bilibili' in song['url'] or 'b23.tv' in song['url']:
                platform = 'bilibili'
            elif 'youtube' in song['url'] or 'youtu.be' in song['url']:
                platform = 'youtube'
            else:
                platform = 'other'

            if not video_id:
                db.update_status(song_id, "error", error_msg="Missing video id", status_detail="Missing video id")
                return

            target_dir = os.path.join(songs_dir, platform, video_id)
            os.makedirs(target_dir, exist_ok=True)

            # If already have instrumental, mark completed
            if song.get('audio_path'):
                inst_full = os.path.join(songs_dir, song['audio_path'])
                if os.path.exists(inst_full):
                    db.update_status(song_id, "completed", 100, status_detail="Ready")
                    return

            raw_audio_path = song.get('raw_audio_path')
            if not raw_audio_path:
                db.update_status(song_id, "error", error_msg="Raw audio missing", status_detail="Raw audio missing")
                return

            full_audio_raw_path = os.path.join(songs_dir, raw_audio_path)

            db.update_status(song_id, "processing", 50, status_detail="Removing Vocals...")

            python_path = config['paths'].get('python_path', 'python')
            vocal_remover_script = config['paths'].get('vocal_remover_script', 'src/vocal_remover.py')
            gpu_id = config.get("processing", {}).get("gpu_id", 0)

            sep_cmd = [
                python_path,
                vocal_remover_script,
                '-i', full_audio_raw_path,
                '-gpu',
                '-id', str(gpu_id),
                '--denoise'
            ]

            inst_filename = "instrumental.mp3"
            inst_path = os.path.join(target_dir, inst_filename)
            sep_cmd.extend(['-o', inst_path])

            ret_sep = run_command_with_progress(sep_cmd, song_id, parse_progress, status_prefix="Separating Vocals")
            if ret_sep != 0:
                db.update_status(song_id, "error", error_msg="Separation Failed", status_detail="Separation Failed")
                return

            rel_inst = os.path.relpath(inst_path, songs_dir)
            db.update_paths(song_id, audio_path=rel_inst)
            db.update_status(song_id, "completed", 100, status_detail="Ready")

            # Save to Library
            db.save_to_library(video_id, video_title, song.get('video_path'), rel_inst, song.get('raw_audio_path'), platform)

        except Exception as e:
            print(f"Task Error: {e}")
            traceback.print_exc()
            db.update_status(song_id, "error", error_msg=str(e))


