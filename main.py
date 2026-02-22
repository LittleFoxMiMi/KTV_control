from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import json
import os
import uvicorn
import re
import subprocess
import time
from fastapi import WebSocket, WebSocketDisconnect
from typing import List

from src.tasks import huey, process_song_task, db

# Load Config
CONFIG_PATH = "config.json"
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
else:
    config = {}

app = FastAPI(title="KTV Control System")

# WebSocket Manager
class ConnectionManager:
    def __init__(self):
        self.players: List[WebSocket] = []
        self.remotes: List[WebSocket] = []

    async def connect(self, websocket: WebSocket, client_type: str):
        await websocket.accept()
        if client_type == 'player':
            self.players.append(websocket)
            print(f"Player Connected. Total: {len(self.players)}")
        else:
            self.remotes.append(websocket)
            print(f"Remote Connected. Total: {len(self.remotes)}")

    def disconnect(self, websocket: WebSocket, client_type: str):
        if client_type == 'player' and websocket in self.players:
            self.players.remove(websocket)
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

@app.get("/songs")
def list_songs():
    songs = db.get_all_songs()
    # Path processing
    for s in songs:
        if s['video_path']:

            try:
                rel = os.path.relpath(s['video_path'], songs_dir).replace("\\", "/")
                s['video_url'] = f"/songs/{rel}"
            except ValueError: s['video_url'] = ""
        
        if s['audio_path']:
             try:
                rel = os.path.relpath(s['audio_path'], songs_dir).replace("\\", "/")
                s['audio_url'] = f"/songs/{rel}"
             except ValueError: s['audio_url'] = ""

        if s.get('raw_audio_path'):
             try:
                rel = os.path.relpath(s['raw_audio_path'], songs_dir).replace("\\", "/")
                s['raw_audio_url'] = f"/songs/{rel}"
             except ValueError: s['raw_audio_url'] = ""

    return songs

@app.get("/song/{song_id}")
def get_song(song_id: int):
    song = db.get_song(song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")
    
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
