import os
import json
import subprocess
import re
import traceback
import time
import sys
import multiprocessing as mp
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from queue import Empty
from huey import SqliteHuey
from huey.api import Task
from src.database import SongDatabase
from src.music_dl import MusicDLService

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
QUEUE_MUSIC_SERIAL = 'music_serial'

DEFAULT_MUSIC_COOKIE_FILE_MAP = {
    'QQMusicClient': 'QQ.txt',
    'NeteaseMusicClient': '163.txt',
    'KuwoMusicClient': '',
    'KugouMusicClient': '',
}

ALL_SOURCES_PROGRESS_RE = re.compile(r'ALL\s+sources.*?\((\d+)\s*/\s*(\d+)\)', re.IGNORECASE)
ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def _parse_all_sources_progress(line: str):
    text = str(line or '').strip()
    if not text:
        return None
    match = ALL_SOURCES_PROGRESS_RE.search(text)
    if not match:
        return None
    completed = int(match.group(1))
    total = int(match.group(2))
    if total <= 0:
        return None
    percent = round((completed / total) * 100, 2)
    return {'completed': completed, 'total': total, 'percent': percent}

class _ProgressCaptureStream:
    def __init__(self, queue_obj):
        self.queue_obj = queue_obj
        self.buffer = ''
        self.last_progress = None

    def write(self, data):
        chunk = str(data or '')
        if not chunk:
            return 0
        try:
            sys.__stdout__.write(chunk)
            sys.__stdout__.flush()
        except Exception:
            pass
        chunk = ANSI_ESCAPE_RE.sub('', chunk)
        self.buffer += chunk
        for match in ALL_SOURCES_PROGRESS_RE.finditer(self.buffer):
            completed = int(match.group(1))
            total = int(match.group(2))
            if total <= 0:
                continue
            percent = round((completed / total) * 100, 2)
            now_key = (completed, total)
            if now_key == self.last_progress:
                continue
            self.last_progress = now_key
            self.queue_obj.put({'type': 'progress', 'completed': completed, 'total': total, 'percent': percent})
        if len(self.buffer) > 4000:
            self.buffer = self.buffer[-800:]
        return len(chunk)

    def flush(self):
        try:
            sys.__stdout__.flush()
        except Exception:
            pass
        if not self.buffer:
            return
        parsed = _parse_all_sources_progress(self.buffer)
        if parsed is not None:
            self.queue_obj.put({'type': 'progress', **parsed})
        self.buffer = ''


def _get_music_cookie_file_map() -> dict:
    cfg_map = ((config.get('music') or {}).get('cookie_file_map') or DEFAULT_MUSIC_COOKIE_FILE_MAP)
    return dict(cfg_map)


def _search_subprocess_entry(keyword: str, mode: str, cookie_map: dict, result_queue):
    try:
        service = MusicDLService()
        enable_qq = mode == 'qq'
        if mode == 'jb':
            sources = ['JBSouMusicClient']
        elif enable_qq:
            sources = ['QQMusicClient']
        else:
            sources = ['NeteaseMusicClient', 'KuwoMusicClient', 'KugouMusicClient']
        progress_stream = _ProgressCaptureStream(result_queue)
        with redirect_stdout(progress_stream), redirect_stderr(progress_stream):
            result = service.search_music(
                keyword=keyword,
                music_sources=sources,
                source_cookie_file_map=cookie_map,
                fast_search=True,
                enable_qq_separate_search=enable_qq,
                sort_mode='original_first',
            )
        progress_stream.flush()
        full_records = {str(k): dict(v) for k, v in service.last_music_records.items()}
        result_queue.put({'type': 'done', 'ok': True, 'result': result, 'full_records': full_records})
    except Exception as exc:
        result_queue.put({'type': 'done', 'ok': False, 'error': str(exc)})


def _safe_json_dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _normalize_text(text: str) -> str:
    return ''.join(str(text or '').lower().split())


def _normalize_source_name(source: str) -> str:
    mapping = {
        'JBSouMusicClient': 'JBSou',
        'QQMusicClient': 'QQ',
        'NeteaseMusicClient': '网易',
        'KuwoMusicClient': '酷我',
        'KugouMusicClient': '酷狗',
    }
    return mapping.get(source or '', source or '-')


def _is_valid_music_record(record: dict, keyword: str) -> bool:
    song_name = str(record.get('song_name') or '').strip()
    if not song_name or song_name.lower() == 'null':
        return False

    text_for_filter = _normalize_text(song_name)
    bad_tokens = ['伴奏', 'instrumental', 'inst', '纯音乐', '网友改编']
    if any(token in text_for_filter for token in bad_tokens):
        return False

    keyword_norm = _normalize_text(keyword)
    singers_norm = _normalize_text(record.get('singers') or '')
    if keyword_norm and (keyword_norm not in text_for_filter) and (keyword_norm not in singers_norm):
        return False

    return True


def _run_parallel_source_search(job_id: str, keyword: str, mode: str, cookie_map: dict):
    result_queue = mp.Queue()
    process = mp.Process(
        target=_search_subprocess_entry,
        args=(keyword, mode, cookie_map, result_queue),
        daemon=True,
    )
    process.start()

    final_payload = None
    while process.is_alive():
        if db.is_music_search_cancel_requested(job_id):
            process.terminate()
            process.join(timeout=1)
            return {'cancelled': True}
        try:
            msg = result_queue.get(timeout=0.2)
            if not isinstance(msg, dict):
                continue
            if msg.get('type') == 'progress':
                total = int(msg.get('total') or 0)
                completed = int(msg.get('completed') or 0)
                percent = float(msg.get('percent') or 0)
                db.update_music_search_job(
                    job_id,
                    progress=percent,
                    total_sources=total,
                    completed_sources=completed,
                    message=f'并行搜索中 {completed}/{total}',
                )
            elif msg.get('type') == 'done':
                final_payload = msg
                break
        except Empty:
            pass
        time.sleep(0.15)

    if process.is_alive():
        process.terminate()
        process.join(timeout=1)

    payload = final_payload
    try:
        while True:
            msg = result_queue.get_nowait()
            if isinstance(msg, dict) and msg.get('type') == 'done':
                payload = msg
            elif isinstance(msg, dict) and msg.get('type') == 'progress':
                total = int(msg.get('total') or 0)
                completed = int(msg.get('completed') or 0)
                percent = float(msg.get('percent') or 0)
                db.update_music_search_job(
                    job_id,
                    progress=percent,
                    total_sources=total,
                    completed_sources=completed,
                    message=f'并行搜索中 {completed}/{total}',
                )
    except Empty:
        pass
    finally:
        result_queue.close()
        result_queue.join_thread()

    if not payload:
        return {'ok': False, 'error': '搜索子进程未返回结果'}
    return payload


def _run_music_search_job(job_id: str, keyword: str, mode: str):
    if mode == 'jb':
        sources = ['JBSouMusicClient']
    elif mode == 'qq':
        sources = ['QQMusicClient']
    else:
        sources = ['NeteaseMusicClient', 'KuwoMusicClient', 'KugouMusicClient']

    cookie_map = _get_music_cookie_file_map()
    total_sources = len(sources)
    db.update_music_search_job(
        job_id,
        mode=mode,
        keyword=keyword,
        status='running',
        message=f'正在搜索 0/{total_sources}',
        progress=0,
        total_sources=total_sources,
        completed_sources=0,
        records_json='[]',
        full_records_json='{}',
        cancel_requested=0,
    )

    merged_records = []
    merged_full_records = {}
    seen_keys = set()
    next_id = 0
    db.update_music_search_job(
        job_id,
        message=f"正在并行搜索（{', '.join([_normalize_source_name(x) for x in sources])}）",
    )

    payload = _run_parallel_source_search(job_id, keyword, mode, cookie_map)
    if payload.get('cancelled'):
        db.update_music_search_job(
            job_id,
            status='cancelled',
            message='已取消',
        )
        return

    if not payload.get('ok'):
        db.update_music_search_job(
            job_id,
            status='error',
            message=f"搜索失败: {payload.get('error', 'unknown error')}",
            progress=100,
            completed_sources=0,
        )
        return

    result_obj = payload.get('result') or {}
    public_records = result_obj.get('records') or []
    full_records = payload.get('full_records') or {}

    for record in public_records:
        if not _is_valid_music_record(record, keyword):
            continue

        dedup_key = (
            _normalize_text(record.get('song_name') or ''),
            _normalize_text(record.get('singers') or ''),
            _normalize_text(record.get('source') or ''),
        )
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        source_id = str(record.get('id'))
        full_record = dict((full_records.get(source_id) or record))

        normalized_public = {
            'id': next_id,
            'singers': record.get('singers', ''),
            'song_name': record.get('song_name', ''),
            'file_size': record.get('file_size', ''),
            'duration': record.get('duration', ''),
            'album': record.get('album', ''),
            'source': record.get('source', ''),
            'ext': record.get('ext', 'mp3'),
        }

        full_record.update(normalized_public)
        merged_records.append(normalized_public)
        merged_full_records[str(next_id)] = full_record
        next_id += 1

    if db.is_music_search_cancel_requested(job_id):
        db.update_music_search_job(job_id, status='cancelled', message='已取消')
        return

    done_message = f"搜索完成，共 {len(merged_records)} 条"
    db.update_music_search_job(
        job_id,
        status='done',
        message=done_message,
        progress=100,
        completed_sources=total_sources,
        records_json=_safe_json_dumps(merged_records),
        full_records_json=_safe_json_dumps(merged_full_records),
    )


def _music_source_to_platform(source: str) -> str:
    mapping = {
        'JBSouMusicClient': 'jbsou',
        'QQMusicClient': 'qq',
        'NeteaseMusicClient': 'netease',
        'KuwoMusicClient': 'kuwo',
        'KugouMusicClient': 'kugou',
    }
    return mapping.get(source, source or 'music')


def _make_music_save_dir(source: str, song_name: str, singers: str) -> str:
    songs_dir = config.get("paths", {}).get("songs_dir", "./songs")
    safe_source = _music_source_to_platform(source)
    folder_name = re.sub(r'[\\/:*?"<>|]+', '_', f"{song_name or 'song'} - {singers or 'unknown'}").strip() or 'song'
    save_dir = os.path.join(songs_dir, 'music', safe_source, folder_name)
    os.makedirs(save_dir, exist_ok=True)
    return save_dir


def _format_size_text_from_bytes(file_size_bytes: int) -> str:
    try:
        size_val = int(file_size_bytes)
    except Exception:
        return ''
    if size_val <= 0:
        return ''
    if size_val >= 1024 ** 3:
        return f"{size_val / (1024 ** 3):.2f}GB"
    if size_val >= 1024 ** 2:
        return f"{size_val / (1024 ** 2):.2f}MB"
    if size_val >= 1024:
        return f"{size_val / 1024:.2f}KB"
    return f"{size_val}B"


@huey.task(queue=QUEUE_MUSIC_SERIAL)
def process_music_download_task(song_id: int, song_record: dict, library_unique_key: str, library_item_id: int = None):
    with _task_lock("music_platform_serial"):
        songs_dir = config.get("paths", {}).get("songs_dir", "./songs")
        os.makedirs(songs_dir, exist_ok=True)
        music_service = MusicDLService()

        try:
            print(f"[MUSIC DOWNLOAD][HUEY] start song_id={song_id} title={(song_record or {}).get('song_name', '')}")
            db.update_status(song_id, 'downloading', 10, status_detail='Downloading Music...')

            save_dir = _make_music_save_dir((song_record or {}).get('source', ''), (song_record or {}).get('song_name', ''), (song_record or {}).get('singers', ''))
            download_result = music_service.download_music_record(
                song_record=song_record,
                save_dir=save_dir,
                cookies_file=None,
                download_cover=True,
                download_lyric=True,
            )

            raw_audio_full_path = download_result['file_path']
            rel_raw_audio = os.path.relpath(raw_audio_full_path, songs_dir)
            db.update_paths(song_id, title=download_result.get('song_name', ''), raw_audio_path=rel_raw_audio)
            db.update_status(song_id, 'processing', 0, status_detail='Dispatching Separation...')
            
            # Offload heavy separation to the QUEUE_SEPARATE worker
            # so that QUEUE_MUSIC_SERIAL (search & download) won't be blocked.
            separate_music_song_task(song_id, download_result, save_dir, library_unique_key, library_item_id)
            
            print(f"[MUSIC DOWNLOAD][HUEY] done song_id={song_id}, dispatched to separation queue.")
        except Exception as exc:
            traceback_text = traceback.format_exc()
            print(f"[MUSIC DOWNLOAD][HUEY] failed song_id={song_id} error={exc}\n{traceback_text}")
            db.update_status(song_id, 'error', 0, error_msg=str(exc), status_detail='Music Download Failed')


@huey.task(queue=QUEUE_SEPARATE)
def separate_music_song_task(song_id: int, download_result: dict, save_dir: str, library_unique_key: str, library_item_id: int = None):
    songs_dir = config.get("paths", {}).get("songs_dir", "./songs")
    try:
        raw_audio_full_path = download_result['file_path']
        db.update_status(song_id, 'processing', 0, status_detail='Removing Vocals...')

        python_path = config.get('paths', {}).get('python_path', 'python')
        vocal_remover_script = config.get('paths', {}).get('vocal_remover_script', 'src/vocal_remover.py')
        gpu_id = config.get('processing', {}).get('gpu_id', 0)
        
        is_player_local = db.get_setting('player_on_localhost') == 'true'
        is_playing = any(s.get('status') == 'completed' for s in db.get_all_songs())
        
        if is_player_local and is_playing:
            vocal_remover_script = config.get('paths', {}).get('new_vocal_remover_script', 'src/new_vocal_remover.py')
            
        instrumental_full_path = os.path.join(save_dir, 'instrumental.mp3')

        sep_cmd = [
            python_path,
            vocal_remover_script,
            '-i', raw_audio_full_path,
            '-gpu',
            '-id', str(gpu_id),
            '--denoise',
            '-o', instrumental_full_path,
        ]
        sep_output_lines = []
        with _task_lock("vocal_separate_gpu"):
            sep_proc = subprocess.Popen(
                sep_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors='replace',
            )
            sep_progress_re = re.compile(r'\[PROGRESS\]\s*(\d+)')
            while True:
                line = sep_proc.stdout.readline() if sep_proc.stdout else ''
                if not line and sep_proc.poll() is not None:
                    break
                if not line:
                    continue
                line = line.strip()
                if line:
                    print(f"[MUSIC DOWNLOAD][HUEY][SEP] {line}")
                    sep_output_lines.append(line)
                    matched = sep_progress_re.search(line)
                    if matched:
                        sep_pct = max(0, min(100, int(matched.group(1))))
                        db.update_status(song_id, 'processing', sep_pct, status_detail='Removing Vocals...')

            sep_return_code = sep_proc.poll()
        if sep_return_code != 0 or not os.path.exists(instrumental_full_path):
            err_text = ('\n'.join(sep_output_lines[-20:]) or 'separation failed').strip()
            raise RuntimeError(f'vocal separation failed: {err_text}')

        rel_audio = os.path.relpath(instrumental_full_path, songs_dir)
        rel_raw_audio = os.path.relpath(raw_audio_full_path, songs_dir)
        rel_cover = os.path.relpath(download_result['cover_path'], songs_dir) if download_result.get('cover_path') else None
        rel_lyric = os.path.relpath(download_result['lyric_path'], songs_dir) if download_result.get('lyric_path') else None

        actual_format = os.path.splitext(instrumental_full_path)[1].lstrip('.').lower()
        if not actual_format:
            actual_format = str(download_result.get('ext', '') or '').strip().lower()

        actual_size_text = ''
        try:
            actual_size_text = _format_size_text_from_bytes(os.path.getsize(instrumental_full_path))
        except Exception:
            actual_size_text = ''
        if not actual_size_text:
            actual_size_text = str(download_result.get('file_size', '') or '')

        db.update_paths(song_id, title=download_result.get('song_name', ''), audio_path=rel_audio, raw_audio_path=rel_raw_audio)
        db.update_song_media(
            song_id=song_id,
            media_type='music',
            singers=download_result.get('singers', ''),
            album=download_result.get('album', ''),
            platform=_music_source_to_platform(download_result.get('source', '')),
            fmt=actual_format,
            size_text=actual_size_text,
            cover_path=rel_cover,
            lyric_path=rel_lyric,
        )
        db.update_status(song_id, 'completed', 100, status_detail='Music Ready')

        lib_id = db.save_music_library_item(
            title=download_result.get('song_name', ''),
            singers=download_result.get('singers', ''),
            album=download_result.get('album', ''),
            platform=_music_source_to_platform(download_result.get('source', '')),
            fmt=actual_format,
            size_text=actual_size_text,
            file_path=rel_audio,
            raw_audio_path=rel_raw_audio,
            cover_path=rel_cover,
            lyric_path=rel_lyric,
            unique_key=library_unique_key,
        )
        if library_item_id:
            db.touch_music_library_item(library_item_id)
        elif lib_id:
            db.touch_music_library_item(lib_id)

    except Exception as exc:
        traceback_text = traceback.format_exc()
        print(f"[MUSIC DOWNLOAD][HUEY] failed separation song_id={song_id} error={exc}\n{traceback_text}")
        db.update_status(song_id, 'error', 0, error_msg=str(exc), status_detail='Separation Failed')

@huey.task(queue=QUEUE_MUSIC_SERIAL)
def process_music_search_jb_task(job_id: str, keyword: str):
    with _task_lock("music_platform_serial"):
        _run_music_search_job(job_id, keyword, mode='jb')


@huey.task(queue=QUEUE_MUSIC_SERIAL)
def process_music_search_normal_task(job_id: str, keyword: str):
    with _task_lock("music_platform_serial"):
        _run_music_search_job(job_id, keyword, mode='normal')


@huey.task(queue=QUEUE_MUSIC_SERIAL)
def process_music_search_qq_task(job_id: str, keyword: str):
    with _task_lock("music_platform_serial"):
        _run_music_search_job(job_id, keyword, mode='qq')

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
                    '-f', 'bestvideo,bestaudio',
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

            db.update_status(song_id, "processing", 0, status_detail="Removing Vocals...")

            python_path = config['paths'].get('python_path', 'python')
            vocal_remover_script = config['paths'].get('vocal_remover_script', 'src/vocal_remover.py')
            gpu_id = config.get("processing", {}).get("gpu_id", 0)
            
            is_player_local = db.get_setting('player_on_localhost') == 'true'
            is_playing = any(s.get('status') == 'completed' for s in db.get_all_songs())
            
            if is_player_local and is_playing:
                vocal_remover_script = config['paths'].get('new_vocal_remover_script', 'src/new_vocal_remover.py')

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

            with _task_lock("vocal_separate_gpu"):
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


