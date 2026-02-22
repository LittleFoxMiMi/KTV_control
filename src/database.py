import sqlite3
import os
import time
from typing import List, Optional, Dict, Any

DB_PATH = os.path.join("database", "ktv.db")

class SongDatabase:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        # check if directory exists
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                title TEXT,
                status TEXT DEFAULT 'pending', -- pending, downloading, processing, completed, error
                status_detail TEXT DEFAULT '', -- detailed status (e.g. "Downloading Video...")
                progress INTEGER DEFAULT 0,
                video_path TEXT,
                audio_path TEXT, -- Instrumental path
                raw_audio_path TEXT, -- Original Audio path
                error_msg TEXT,
                created_at REAL,
                priority INTEGER DEFAULT 0,
                video_id TEXT UNIQUE
            )
        ''')
        
        # Migration: Check if priority column exists
        try:
            cursor.execute("SELECT priority FROM songs LIMIT 1")
        except sqlite3.OperationalError:
            print("Migrating DB: Adding priority column")
            try:
                cursor.execute("ALTER TABLE songs ADD COLUMN priority INTEGER DEFAULT 0")
                cursor.execute("UPDATE songs SET priority = id")
            except Exception as e:
                print(f"Migration priority failed: {e}")

        # Migration: Check if video_id column exists
        try:
            cursor.execute("SELECT video_id FROM songs LIMIT 1")
        except sqlite3.OperationalError:
            print("Migrating DB: Adding video_id column")
            try:
                cursor.execute("ALTER TABLE songs ADD COLUMN video_id TEXT UNIQUE")
            except Exception as e:
                print(f"Migration video_id failed: {e}")

        # Library Table for History/Cache
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS library (
                video_id TEXT PRIMARY KEY,
                title TEXT,
                video_path TEXT,
                audio_path TEXT,
                raw_audio_path TEXT,
                platform TEXT,
                created_at REAL,
                last_used_at REAL
            )
        ''')
        
        # Migration: Create settings table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
            
        conn.commit()
        conn.close()

    def get_library_song(self, video_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM library WHERE video_id = ?", (video_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def save_to_library(self, video_id: str, title: str, video_path: str, audio_path: str, raw_audio_path: str, platform: str):
        conn = self._get_conn()
        cursor = conn.cursor()
        now = time.time()
        try:
            cursor.execute('''
                INSERT OR REPLACE INTO library (video_id, title, video_path, audio_path, raw_audio_path, platform, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM library WHERE video_id=?), ?), ?)
            ''', (video_id, title, video_path, audio_path, raw_audio_path, platform, video_id, now, now))
            conn.commit()
        except Exception as e:
            print(f"Library Save Error: {e}")
        finally:
            conn.close()

    def get_setting(self, key: str, default: Any = None) -> Any:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return row['value']
        return default

    def set_setting(self, key: str, value: Any):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        conn.close()

    def get_all_state(self) -> Dict[str, Any]:
        """Retrieve all control state settings"""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings")
        rows = cursor.fetchall()
        conn.close()
        return {r['key']: r['value'] for r in rows}

    def add_song(self, url: str, video_id: str = None, title: str = None) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            # Check by video_id if provided
            if video_id:
                cursor.execute("SELECT id FROM songs WHERE video_id = ?", (video_id,))
                row = cursor.fetchone()
                if row:
                    return row['id']

            # Check by URL
            cursor.execute("SELECT id FROM songs WHERE url = ?", (url,))
            row = cursor.fetchone()
            if row:
                # If we have a video_id now but DB didn't, update it
                if video_id:
                     cursor.execute("UPDATE songs SET video_id = ? WHERE id = ?", (video_id, row['id']))
                     conn.commit()
                return row['id']
            
            # Insert
            cursor.execute('''
                INSERT INTO songs (url, video_id, title, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
            ''', (url, video_id, title, time.time()))
            conn.commit()
            return cursor.lastrowid
        except Exception as e:
            print(f"DB Error: {e}")
            return -1
        finally:
            conn.close()

    def get_song_by_id(self, song_id: int):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM songs WHERE id = ?", (song_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def update_status(self, song_id: int, status: str, progress: int = 0, error_msg: str = None, status_detail: str = None):
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            updates = ["status = ?", "progress = ?"]
            params = [status, progress]
            
            if error_msg:
                updates.append("error_msg = ?")
                params.append(error_msg)
            
            if status_detail:
                updates.append("status_detail = ?")
                params.append(status_detail)
            
            params.append(song_id)
            
            cursor.execute(f'''
                UPDATE songs 
                SET {", ".join(updates)}
                WHERE id = ?
            ''', params)
            conn.commit()
        finally:
            conn.close()

    def update_paths(self, song_id: int, title: str = None, video_path: str = None, audio_path: str = None, raw_audio_path: str = None, video_id: str = None):
        conn = self._get_conn()
        cursor = conn.cursor()
        updates = []
        params = []
        
        try:
            if title:
                updates.append("title = ?")
                params.append(title)
            if video_path:
                updates.append("video_path = ?")
                params.append(video_path)
            if audio_path:
                updates.append("audio_path = ?")
                params.append(audio_path) 
            if raw_audio_path:
                updates.append("raw_audio_path = ?")
                params.append(raw_audio_path)
            if video_id:
                updates.append("video_id = ?")
                params.append(video_id)
                
            if not updates:
                return

            params.append(song_id)
            cursor.execute(f"UPDATE songs SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()
        finally:
            conn.close()

    def get_song(self, song_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM songs WHERE id = ?", (song_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return dict(row)
        return None

    def get_all_songs(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        # ORDER BY priority DESC (Higher priority first), then ID ASC (Oldest first - FIFO)
        # This ensures normal queue behavior (First In, First Out) unless Priority is manually changed.
        cursor.execute("SELECT * FROM songs ORDER BY priority DESC, id ASC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def move_song(self, song_id: int, direction: str) -> bool:
        """
        Move song up/down/top.
        UP = Higher Priority (Earlier in queue).
        DOWN = Lower Priority (Later in queue).
        TOP = Move directly to queue top.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            # Get current song
            cursor.execute("SELECT id, priority FROM songs WHERE id = ?", (song_id,))
            current = cursor.fetchone()
            if not current: return False
            
            # Fetch all in effective order to find neighbor
            cursor.execute("SELECT id, priority FROM songs ORDER BY priority DESC, id ASC")
            all_songs = [dict(r) for r in cursor.fetchall()]
            
            idx = next((i for i, s in enumerate(all_songs) if s['id'] == song_id), -1)
            if idx == -1: return False
            
            target_idx = -1
            if direction == 'up' and idx > 0:
                target_idx = idx - 1
            elif direction == 'down' and idx < len(all_songs) - 1:
                target_idx = idx + 1

            if direction == 'top':
                # Keep index 0 as active slot (now playing / processing).
                # 'Top' means top of pending queue, i.e. index 1.
                if idx <= 1:
                    return False
                moving = all_songs.pop(idx)
                all_songs.insert(1, moving)

                count = len(all_songs)
                for i, s in enumerate(all_songs):
                    new_p = count - i
                    if s['priority'] != new_p:
                        cursor.execute("UPDATE songs SET priority = ? WHERE id = ?", (new_p, s['id']))
                conn.commit()
                return True
                
            if target_idx != -1:
                neighbor = all_songs[target_idx]
                
                # If priorities match, we need to create a priority gap to swap them
                if current['priority'] == neighbor['priority']:
                    # Re-normalize entire list priorities to be their reverse index
                    # This assigns unique explicit priority to every song
                    # Top of list = Highest Priority (count), Bottom = 0
                    
                    # First perform the swap in memory list
                    all_songs[idx], all_songs[target_idx] = all_songs[target_idx], all_songs[idx]
                    
                    # Update DB
                    count = len(all_songs)
                    for i, s in enumerate(all_songs):
                        new_p = count - i
                        if s['priority'] != new_p:
                             cursor.execute("UPDATE songs SET priority = ? WHERE id = ?", (new_p, s['id']))
                else:
                    # Simple swap of priority values works if they are distinct
                    cursor.execute("UPDATE songs SET priority = ? WHERE id = ?", (neighbor['priority'], current['id']))
                    cursor.execute("UPDATE songs SET priority = ? WHERE id = ?", (current['priority'], neighbor['id']))
                
                conn.commit()
                return True
            return False
        except Exception as e:
            print(f"Move failed: {e}")
            return False
        finally:
            conn.close()

    def delete_song(self, song_id: int):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT video_path, audio_path FROM songs WHERE id=?", (song_id,))
        row = cursor.fetchone()
        if row:
            if row['video_path'] and os.path.exists(row['video_path']):
                try: os.remove(row['video_path'])
                except: pass
            if row['audio_path'] and os.path.exists(row['audio_path']):
                try: os.remove(row['audio_path'])
                except: pass
        
        cursor.execute("DELETE FROM songs WHERE id = ?", (song_id,))
        conn.commit()
        conn.close()
