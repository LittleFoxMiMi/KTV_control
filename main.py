from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import json
import os
import uvicorn
import re
import time
import uuid
from fastapi import WebSocket, WebSocketDisconnect
from typing import List, Dict, Any

from src.tasks import (
    huey,
    process_song_task,
    process_music_search_jb_task,
    process_music_search_normal_task,
    process_music_search_qq_task,
    process_music_download_task,
    db,
)
from src.music_dl import MusicDLService

# Load Config
CONFIG_PATH = "config.json"
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
else:
    config = {}

app = FastAPI(title="KTV Control System")

music_service = MusicDLService()
DEBUG_LOG_DIR = os.path.join('Temp', 'logs')
DEBUG_MUSIC_DOWNLOAD_LOG = os.path.join(DEBUG_LOG_DIR, 'music_download.log')


def _task_to_debug_dict(task_obj):
    if task_obj is None:
        return {}
    return {
        'id': getattr(task_obj, 'id', None),
        'name': getattr(task_obj, 'name', None),
        'args': list(getattr(task_obj, 'args', []) or []),
        'kwargs': dict(getattr(task_obj, 'kwargs', {}) or {}),
        'eta': str(getattr(task_obj, 'eta', '')),
        'priority': getattr(task_obj, 'priority', None),
        'repr': repr(task_obj),
    }


def _huey_snapshot(limit: int = 50):
    pending = huey.pending(limit=limit)
    scheduled = huey.scheduled(limit=limit)
    snapshot = {
        'pending_count': huey.pending_count(),
        'scheduled_count': huey.scheduled_count(),
        'result_count': huey.result_count(),
        'pending': [_task_to_debug_dict(x) for x in pending],
        'scheduled': [_task_to_debug_dict(x) for x in scheduled],
    }
    print(f"[HUEY DEBUG] pending={snapshot['pending_count']} scheduled={snapshot['scheduled_count']} result={snapshot['result_count']}")
    for item in snapshot['pending']:
        print(f"[HUEY DEBUG][PENDING] id={item.get('id')} name={item.get('name')} args={item.get('args')}")
    for item in snapshot['scheduled']:
        print(f"[HUEY DEBUG][SCHEDULED] id={item.get('id')} name={item.get('name')} eta={item.get('eta')}")
    return snapshot

# WebSocket Manager
class ConnectionManager:
    def __init__(self):
        self.players: List[WebSocket] = []
        self.remotes: List[WebSocket] = []

    def _update_localhost_player_state(self):
        has_local = False
        for p in self.players:
            host = p.client.host
            if host in ['127.0.0.1', 'localhost', '::1']:
                has_local = True
                break
        db.set_setting('player_on_localhost', 'true' if has_local else 'false')

    async def connect(self, websocket: WebSocket, client_type: str):
        await websocket.accept()
        if client_type == 'player':
            self.players.append(websocket)
            self._update_localhost_player_state()
            print(f"Player Connected. Total: {len(self.players)}")
        else:
            self.remotes.append(websocket)
            print(f"Remote Connected. Total: {len(self.remotes)}")

    def disconnect(self, websocket: WebSocket, client_type: str):
        if client_type == 'player' and websocket in self.players:
            self.players.remove(websocket)
            self._update_localhost_player_state()
        elif client_type != 'player' and websocket in self.remotes:
            self.remotes.remove(websocket)

    async def send_to_players(self, message: str):
        for connection in self.players:
            try:
                await connection.send_text(message)
            except:
                pass

    async def send_to_remotes(self, message: str, exclude: WebSocket = None):
        for connection in self.remotes:
            if connection == exclude: continue
            try:
                await connection.send_text(message)
            except:
                pass

manager = ConnectionManager()

# CORS for Vue frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve songs directory
songs_dir = config.get("paths", {}).get("songs_dir", "./songs")
os.makedirs(songs_dir, exist_ok=True)
app.mount("/songs", StaticFiles(directory=songs_dir), name="songs")

class SongRequest(BaseModel):
    url: str


class MusicSearchStartRequest(BaseModel):
    keyword: str
    mode: str = 'normal'  # normal | qq


class MusicSearchQueueRequest(BaseModel):
    job_id: str
    result_id: int


class MusicLibraryQueueRequest(BaseModel):
    library_id: int


class MVLibraryQueueRequest(BaseModel):
    video_id: str


def _normalize_rel_path(path_value: str) -> str:
    if not path_value:
        return ''
    return str(path_value).replace('\\', '/')


def _to_full_media_path(path_value: str) -> str:
    if not path_value:
        return ''
    path_text = str(path_value)
    if os.path.isabs(path_text):
        return path_text
    return os.path.join(songs_dir, path_text)


def _song_missing_required_files(song: Dict[str, Any]) -> bool:
    media_type = song.get('media_type') or 'video'
    required_keys = ['audio_path'] if media_type == 'music' else ['video_path', 'audio_path']
    for key in required_keys:
        rel_path = song.get(key)
        if not rel_path:
            return True
        full_path = _to_full_media_path(rel_path)
        if not os.path.exists(full_path):
            return True
    return False


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
    safe_source = _music_source_to_platform(source)
    folder_name = re.sub(r'[\\/:*?"<>|]+', '_', f"{song_name or 'song'} - {singers or 'unknown'}").strip() or 'song'
    save_dir = os.path.join(songs_dir, 'music', safe_source, folder_name)
    os.makedirs(save_dir, exist_ok=True)
    return save_dir


def _build_music_public_record(item: Dict[str, Any], from_library: bool = False) -> Dict[str, Any]:
    return {
        'id': item.get('id', -1),
        'song_name': item.get('song_name') or item.get('title') or '',
        'singers': item.get('singers', ''),
        'album': item.get('album', ''),
        'source': item.get('source') or item.get('platform') or '',
        'ext': item.get('ext') or item.get('format') or '',
        'file_size': item.get('file_size') or item.get('size_text') or '',
        'from_library': from_library,
        'library_id': item.get('id') if from_library else None,
    }


def _build_mv_library_public_record(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'id': -1,
        'song_name': item.get('title') or '',
        'singers': '',
        'album': '',
        'source': item.get('platform') or 'mv',
        'ext': 'video',
        'file_size': '',
        'from_library': True,
        'library_id': None,
        'library_type': 'mv',
        'video_id': item.get('video_id') or '',
    }


def _parse_json_safe(raw_text: str, fallback):
    try:
        if raw_text is None:
            return fallback
        return json.loads(raw_text)
    except Exception:
        return fallback


@app.websocket("/ws/{client_type}")
async def websocket_endpoint(websocket: WebSocket, client_type: str):
    await manager.connect(websocket, client_type)
    try:
        while True:
            data = await websocket.receive_text()
            # Remote -> Player (Control) AND Other Remotes (Sync)
            # Player -> All Remotes (Status)
            if client_type == 'remote':
                await manager.send_to_players(data)
                await manager.send_to_remotes(data, exclude=websocket)
            else:
                 await manager.send_to_remotes(data)
    except WebSocketDisconnect:
        manager.disconnect(websocket, client_type)

@app.get("/")
def read_root():
    return {"status": "online", "system": "KTV Control Backend", "worker_hint": "Run 'huey_consumer.py src.tasks.huey' to start task processor"}

def extract_video_id(url):
    """Attempt to extract video ID from common platforms for fast library lookup"""
    # YouTube
    if "youtube.com" in url or "youtu.be" in url:
        match = re.search(r'[?&]v=([^&]+)', url)
        if match: return match.group(1), 'youtube'
        match = re.search(r'youtu\.be/([^?]+)', url)
        if match: return match.group(1), 'youtube'
    # Bilibili
    if "bilibili.com" in url:
        match = re.search(r'/video/(BV[a-zA-Z0-9]+)', url)
        if match: return match.group(1), 'bilibili'
    return None, None

@app.post("/add_song")
def add_song(request: SongRequest):
    # check if url is supported or not empty
    if not request.url:
        raise HTTPException(status_code=400, detail="URL is required")
    
    # Extract URL if embedded in text (e.g. Bilibili share text)
    match = re.search(r'https?://[^\s]+', request.url)
    clean_url = match.group(0) if match else request.url.strip()

    # Attempt ID extraction for library check
    try_vid, try_platform = None, None  # extract_video_id(clean_url)

    # Add to DB immediately
    # If using regex ID, pass it to help DB matching
    song_id = db.add_song(clean_url, video_id=try_vid, title=clean_url)
    
    if song_id == -1:
         raise HTTPException(status_code=500, detail="Database Error")

    # If it returns an existing song ID, check status
    existing_song = db.get_song(song_id)
    if existing_song['status'] == 'completed':
         return {"status": "queued", "id": song_id, "title": existing_song.get('title'), "message": "Song already exists"}

    # Library Access Optimization
    # Code Disabled to prevent incorrect ID matching (Particularly for multipart videos)
    # The worker task will resolve the correct ID
    '''
    if try_vid:
        lib_entry = db.get_library_song(try_vid)
        if lib_entry:
            # Check files existence
            f_vid = os.path.join(songs_dir, lib_entry['video_path']) if lib_entry['video_path'] else None
            f_aud = os.path.join(songs_dir, lib_entry['audio_path']) if lib_entry['audio_path'] else None
            
            if f_vid and os.path.exists(f_vid) and f_aud and os.path.exists(f_aud):
                 # Fast Track success
                 db.update_paths(song_id, 
                                 video_path=lib_entry['video_path'], 
                                 audio_path=lib_entry['audio_path'], 
                                 raw_audio_path=lib_entry['raw_audio_path'],
                                 title=lib_entry['title'])
                 db.update_status(song_id, "completed", 100, status_detail="Instant from Library")
                 
                 # Touch library entry
                 db.save_to_library(try_vid, lib_entry['title'], lib_entry['video_path'], lib_entry['audio_path'], lib_entry['raw_audio_path'], try_platform)
                 
                 return {"status": "queued", "id": song_id, "title": lib_entry['title'], "message": "Restored from Library"}
    '''

    process_song_task(song_id)
    return {"status": "queued", "id": song_id, "title": clean_url}


@app.get('/music/search/local')
def music_search_local(keyword: str):
    keyword = (keyword or '').strip()
    if not keyword:
        raise HTTPException(status_code=400, detail='keyword is required')
    music_items_raw = db.search_music_library(keyword)
    mv_items_raw = db.search_mv_library(keyword)

    music_items = []
    for item in music_items_raw:
        file_path = item.get('file_path')
        if file_path and os.path.exists(_to_full_media_path(file_path)):
            music_items.append(item)
        else:
            item_id = item.get('id')
            if item_id:
                db.delete_music_library_item(int(item_id))

    mv_items = []
    for item in mv_items_raw:
        video_path = item.get('video_path')
        audio_path = item.get('audio_path')
        has_video = bool(video_path) and os.path.exists(_to_full_media_path(video_path))
        has_audio = bool(audio_path) and os.path.exists(_to_full_media_path(audio_path))
        if has_video and has_audio:
            mv_items.append(item)
        else:
            video_id = item.get('video_id')
            if video_id:
                db.delete_mv_library_song(str(video_id))

    merged_records = [_build_music_public_record(item, from_library=True) for item in music_items]
    for rec in merged_records:
        rec['library_type'] = 'music'
    merged_records.extend([_build_mv_library_public_record(item) for item in mv_items])

    return {
        'keyword': keyword,
        'records': merged_records,
        'from_library_only': True,
        'total': len(merged_records),
    }


@app.post('/music/search/start')
def music_search_start(req: MusicSearchStartRequest):
    keyword = (req.keyword or '').strip()
    mode = (req.mode or 'normal').strip().lower()
    if mode not in ('jb', 'normal', 'qq'):
        raise HTTPException(status_code=400, detail='mode must be jb, normal or qq')
    if not keyword:
        raise HTTPException(status_code=400, detail='keyword is required')

    stale_jobs = db.find_stale_music_search_jobs(waiting_seconds=20, running_seconds=1800)
    if stale_jobs:
        stale_ids = [str(item.get('job_id')) for item in stale_jobs if item.get('job_id')]
        stale_task_ids = [str(item.get('task_id')) for item in stale_jobs if item.get('task_id')]
        for task_id in stale_task_ids:
            try:
                huey.revoke_by_id(task_id)
            except Exception:
                pass
        db.mark_music_search_jobs_cancelled(stale_ids, message='自动清理旧排队任务')

    ahead = db.count_active_music_search_jobs()

    job_id = str(uuid.uuid4())
    db.create_music_search_job(job_id=job_id, mode=mode, keyword=keyword)
    try:
        if mode == 'jb':
            task = process_music_search_jb_task(job_id, keyword)
        elif mode == 'qq':
            task = process_music_search_qq_task(job_id, keyword)
        else:
            task = process_music_search_normal_task(job_id, keyword)
        task_id = getattr(task, 'id', None)
        if task_id:
            db.set_music_search_task_id(job_id, task_id)
        _huey_snapshot(limit=20)
    except Exception as exc:
        db.update_music_search_job(job_id, status='error', message=f'提交任务失败: {exc}')
        raise HTTPException(status_code=500, detail='failed to enqueue search task')

    return {'job_id': job_id, 'mode': mode, 'status': 'waiting', 'ahead': ahead}


@app.get('/music/search/job/{job_id}')
def music_search_job(job_id: str):
    job = db.get_music_search_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='search job not found')
    ahead = db.get_music_search_job_ahead(job_id)

    return {
        'job_id': job['job_id'],
        'mode': job.get('mode') or 'normal',
        'keyword': job.get('keyword') or '',
        'status': job.get('status') or 'waiting',
        'message': job.get('message') or '',
        'ahead': ahead,
        'progress': float(job.get('progress') or 0),
        'total_sources': int(job.get('total_sources') or 0),
        'completed_sources': int(job.get('completed_sources') or 0),
        'records': _parse_json_safe(job.get('records_json'), []),
    }


@app.post('/music/search/job/{job_id}/cancel')
def music_search_cancel(job_id: str):
    job = db.get_music_search_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='search job not found')

    db.update_music_search_job(job_id, cancel_requested=1, status='cancelled', message='已取消')
    task_id = job.get('task_id')
    if task_id:
        try:
            huey.revoke_by_id(task_id)
        except Exception:
            pass
    return {'status': 'cancelled'}


@app.post('/music/queue/from_search')
def music_queue_from_search(req: MusicSearchQueueRequest):
    job = db.get_music_search_job(req.job_id)
    if not job:
        raise HTTPException(status_code=404, detail='search job not found')
    if (job.get('status') or '') != 'done':
        raise HTTPException(status_code=400, detail='search job not completed')

    full_records = _parse_json_safe(job.get('full_records_json'), {})
    full_record = (full_records or {}).get(str(req.result_id))
    if not full_record:
        raise HTTPException(status_code=404, detail='search record not found')

    title = full_record.get('song_name') or 'music'
    platform = _music_source_to_platform(full_record.get('source', ''))
    pseudo_url = f"musicsearch://{platform}/{int(time.time() * 1000)}/{uuid.uuid4().hex[:8]}"
    song_id = db.add_song(pseudo_url, title=title)
    if song_id == -1:
        raise HTTPException(status_code=500, detail='Database Error')

    db.update_song_media(
        song_id=song_id,
        media_type='music',
        singers=full_record.get('singers', ''),
        album=full_record.get('album', ''),
        platform=platform,
        fmt=full_record.get('ext', ''),
        size_text=full_record.get('file_size', ''),
    )

    library_unique_key = f"{platform}:{title}:{full_record.get('singers', '')}:{full_record.get('album', '')}:{full_record.get('ext', '')}"
    process_music_download_task(song_id, full_record, library_unique_key)
    _huey_snapshot(limit=20)
    return {'status': 'queued', 'id': song_id, 'title': title}


@app.post('/music/queue/from_library')
def music_queue_from_library(req: MusicLibraryQueueRequest):
    item = db.get_music_library_item(req.library_id)
    if not item:
        raise HTTPException(status_code=404, detail='library item not found')

    required_path = item.get('file_path')
    if not required_path or not os.path.exists(_to_full_media_path(required_path)):
        db.delete_music_library_item(req.library_id)
        raise HTTPException(status_code=404, detail='no file')

    pseudo_url = f"musiclib://{req.library_id}/{int(time.time() * 1000)}"
    song_id = db.add_song(pseudo_url, title=item.get('title') or 'music')
    if song_id == -1:
        raise HTTPException(status_code=500, detail='Database Error')

    db.update_paths(
        song_id,
        title=item.get('title', ''),
        audio_path=item.get('file_path'),
        raw_audio_path=item.get('raw_audio_path') or item.get('file_path'),
    )
    db.update_song_media(
        song_id=song_id,
        media_type='music',
        singers=item.get('singers', ''),
        album=item.get('album', ''),
        platform=item.get('platform', ''),
        fmt=item.get('format', ''),
        size_text=item.get('size_text', ''),
        cover_path=item.get('cover_path'),
        lyric_path=item.get('lyric_path'),
    )
    db.update_status(song_id, 'completed', 100, status_detail='Restored from Music Library')
    db.touch_music_library_item(req.library_id)
    return {'status': 'queued', 'id': song_id, 'title': item.get('title')}


@app.post('/music/queue/from_mv_library')
def music_queue_from_mv_library(req: MVLibraryQueueRequest):
    video_id = (req.video_id or '').strip()
    if not video_id:
        raise HTTPException(status_code=400, detail='video_id is required')

    item = db.get_library_song(video_id)
    if not item:
        raise HTTPException(status_code=404, detail='mv library item not found')

    required_video = item.get('video_path')
    required_audio = item.get('audio_path')
    if (not required_video or not os.path.exists(_to_full_media_path(required_video))
            or not required_audio or not os.path.exists(_to_full_media_path(required_audio))):
        db.delete_mv_library_song(video_id)
        raise HTTPException(status_code=404, detail='no file')

    pseudo_url = f"mvlib://{video_id}/{int(time.time() * 1000)}"
    song_id = db.add_song(pseudo_url, video_id=video_id, title=item.get('title') or 'video')
    if song_id == -1:
        raise HTTPException(status_code=500, detail='Database Error')

    db.update_paths(
        song_id,
        title=item.get('title', ''),
        video_path=item.get('video_path'),
        audio_path=item.get('audio_path'),
        raw_audio_path=item.get('raw_audio_path') or item.get('video_path'),
        video_id=video_id,
    )
    db.update_song_media(
        song_id=song_id,
        media_type='video',
        platform=item.get('platform', ''),
    )
    db.update_status(song_id, 'completed', 100, status_detail='Restored from MV Library')
    db.save_to_library(
        video_id,
        item.get('title', ''),
        item.get('video_path'),
        item.get('audio_path'),
        item.get('raw_audio_path'),
        item.get('platform', ''),
    )
    return {'status': 'queued', 'id': song_id, 'title': item.get('title')}


@app.get('/debug/huey')
def debug_huey(limit: int = 50):
    limit = max(1, min(200, int(limit or 50)))
    return _huey_snapshot(limit=limit)


@app.get('/debug/music_download_log')
def debug_music_download_log(lines: int = 200):
    lines = max(10, min(2000, int(lines or 200)))
    if not os.path.exists(DEBUG_MUSIC_DOWNLOAD_LOG):
        return {'exists': False, 'lines': []}
    with open(DEBUG_MUSIC_DOWNLOAD_LOG, 'r', encoding='utf-8', errors='ignore') as fp:
        all_lines = fp.readlines()
    return {'exists': True, 'lines': [x.rstrip('\n') for x in all_lines[-lines:]]}

@app.get("/songs")
def list_songs():
    songs = db.get_all_songs()
    filtered_songs = []

    for s in songs:
        if (s.get('status') or '') == 'completed' and _song_missing_required_files(s):
            try:
                db.update_status(s['id'], 'error', 0, error_msg='no file', status_detail='no file')
            except Exception:
                pass
            db.delete_song(s['id'])
            print(f"[QUEUE CLEANUP] removed missing file song id={s.get('id')}")
            continue
        filtered_songs.append(s)

    songs = filtered_songs
    # Path processing
    for s in songs:
        s['media_type'] = s.get('media_type') or 'video'
        s['format'] = s.get('format') or ''
        s['size_text'] = s.get('size_text') or ''
        s['platform'] = s.get('platform') or ''
        s['singers'] = s.get('singers') or ''
        s['album'] = s.get('album') or ''
        if s['video_path']:
            try:
                rel = os.path.relpath(s['video_path'], songs_dir).replace("\\", "/")
                s['video_url'] = f"/songs/{rel}"
            except ValueError:
                s['video_url'] = ""

        if s['audio_path']:
            try:
                rel = os.path.relpath(s['audio_path'], songs_dir).replace("\\", "/")
                s['audio_url'] = f"/songs/{rel}"
            except ValueError:
                s['audio_url'] = ""

        if s.get('raw_audio_path'):
            try:
                rel = os.path.relpath(s['raw_audio_path'], songs_dir).replace("\\", "/")
                s['raw_audio_url'] = f"/songs/{rel}"
            except ValueError:
                s['raw_audio_url'] = ""

        if s.get('cover_path'):
            try:
                rel = os.path.relpath(s['cover_path'], songs_dir).replace("\\", "/")
                s['cover_url'] = f"/songs/{rel}"
            except ValueError:
                s['cover_url'] = ""

        if s.get('lyric_path'):
            try:
                rel = os.path.relpath(s['lyric_path'], songs_dir).replace("\\", "/")
                s['lyric_url'] = f"/songs/{rel}"
            except ValueError:
                s['lyric_url'] = ""

    return songs

@app.get("/song/{song_id}")
def get_song(song_id: int):
    song = db.get_song(song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")

    if (song.get('status') or '') == 'completed' and _song_missing_required_files(song):
        try:
            db.update_status(song_id, 'error', 0, error_msg='no file', status_detail='no file')
        except Exception:
            pass
        db.delete_song(song_id)
        raise HTTPException(status_code=404, detail='no file')
    
    # Inject URLs
    if song['video_path']:
        try:
             rel = os.path.relpath(song['video_path'], songs_dir).replace("\\", "/")
             song['video_url'] = f"/songs/{rel}"
        except: pass
    if song['audio_path']:
        try:
             rel = os.path.relpath(song['audio_path'], songs_dir).replace("\\", "/")
             song['audio_url'] = f"/songs/{rel}"
        except: pass
    if song.get('raw_audio_path'):
        try:
             rel = os.path.relpath(song['raw_audio_path'], songs_dir).replace("\\", "/")
             song['raw_audio_url'] = f"/songs/{rel}"
        except: pass
    if song.get('cover_path'):
        try:
             rel = os.path.relpath(song['cover_path'], songs_dir).replace("\\", "/")
             song['cover_url'] = f"/songs/{rel}"
        except: pass
    if song.get('lyric_path'):
        try:
             rel = os.path.relpath(song['lyric_path'], songs_dir).replace("\\", "/")
             song['lyric_url'] = f"/songs/{rel}"
        except: pass

    return song

class MoveRequest(BaseModel):
    direction: str

@app.post("/song/{song_id}/move")
def move_song(song_id: int, request: MoveRequest):
    if request.direction not in ['up', 'down', 'top']:
        raise HTTPException(status_code=400, detail="Invalid direction")
    
    success = db.move_song(song_id, request.direction)
    if not success:
         raise HTTPException(status_code=400, detail="Move failed")
    
    return {"message": "Moved", "status": "ok"}

@app.delete("/song/{song_id}")
def delete_song(song_id: int):
    db.delete_song(song_id)
    return {"message": "Deleted"}

class StateUpdate(BaseModel):
    key: str
    value: str

@app.get("/state")
def get_state():
    return db.get_all_state()

@app.post("/state")
def update_state(update: StateUpdate):
    db.set_setting(update.key, update.value)
    return {"status": "ok"}

if __name__ == "__main__":
    print("----------------------------------------------------------------")
    print("Starting KTV Backend API")
    print(f"Songs directory served at: {songs_dir}")
    print("IMPORTANT: Open a separate terminal and run:")
    print("   .\\python\\python.exe worker.py")
    print("----------------------------------------------------------------")
    
    host = config.get("system", {}).get("host", "0.0.0.0")
    port = config.get("system", {}).get("port", 8000)
    # Fix for stuck shutdown on Windows
    try:
        uvicorn.run(app, host=host, port=port, timeout_graceful_shutdown=1)
    except KeyboardInterrupt:
        pass
