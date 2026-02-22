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

        def _ensure_column(table_name: str, column_name: str, column_def: str):
            try:
                cursor.execute(f"PRAGMA table_info({table_name})")
                existed_cols = {row['name'] for row in cursor.fetchall()}
                if column_name in existed_cols:
                    return
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
            except sqlite3.OperationalError as e:
                err = str(e).lower()
                if 'duplicate column name' in err:
                    return
                print(f"Migration {column_name} failed: {e}")
            except Exception as e:
                print(f"Migration {column_name} failed: {e}")

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
                video_id TEXT UNIQUE,
                media_type TEXT DEFAULT 'video',
                singers TEXT,
                platform TEXT,
                format TEXT,
                size_text TEXT,
                cover_path TEXT,
                lyric_path TEXT
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

        # Migration: music related columns
        song_migrations = [
            ("media_type", "TEXT DEFAULT 'video'"),
            ("singers", "TEXT"),
            ("platform", "TEXT"),
            ("format", "TEXT"),
            ("size_text", "TEXT"),
            ("album", "TEXT"),
            ("cover_path", "TEXT"),
            ("lyric_path", "TEXT"),
        ]
        for col_name, col_def in song_migrations:
            _ensure_column('songs', col_name, col_def)

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

        # Music Library Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS music_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                singers TEXT,
                album TEXT,
                platform TEXT,
                format TEXT,
                size_text TEXT,
                file_path TEXT NOT NULL,
                raw_audio_path TEXT,
                cover_path TEXT,
                lyric_path TEXT,
                unique_key TEXT UNIQUE,
                created_at REAL,
                last_used_at REAL
            )
        ''')

        # Music Search Jobs Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS music_search_jobs (
                job_id TEXT PRIMARY KEY,
                task_id TEXT,
                mode TEXT,
                keyword TEXT,
                status TEXT,
                message TEXT,
                progress REAL DEFAULT 0,
                total_sources INTEGER DEFAULT 0,
                completed_sources INTEGER DEFAULT 0,
                records_json TEXT,
                full_records_json TEXT,
                cancel_requested INTEGER DEFAULT 0,
                created_at REAL,
                updated_at REAL
            )
        ''')

        _ensure_column('music_library', 'raw_audio_path', 'TEXT')
        _ensure_column('music_library', 'album', 'TEXT')
        _ensure_column('music_search_jobs', 'task_id', 'TEXT')
        _ensure_column('music_search_jobs', 'progress', 'REAL DEFAULT 0')
        _ensure_column('music_search_jobs', 'total_sources', 'INTEGER DEFAULT 0')
        _ensure_column('music_search_jobs', 'completed_sources', 'INTEGER DEFAULT 0')
        _ensure_column('music_search_jobs', 'records_json', 'TEXT')
        _ensure_column('music_search_jobs', 'full_records_json', 'TEXT')
        _ensure_column('music_search_jobs', 'cancel_requested', 'INTEGER DEFAULT 0')
        _ensure_column('music_search_jobs', 'created_at', 'REAL')
        _ensure_column('music_search_jobs', 'updated_at', 'REAL')

        self._cleanup_music_search_jobs(cursor, max_age_seconds=24 * 3600, keep_latest=300)
            
        conn.commit()
        conn.close()

    def _cleanup_music_search_jobs(self, cursor, max_age_seconds: int = 86400, keep_latest: int = 300):
        now = time.time()
        expire_before = now - max_age_seconds
        try:
            cursor.execute(
                '''
                DELETE FROM music_search_jobs
                WHERE COALESCE(updated_at, created_at, 0) < ?
                ''',
                (expire_before,),
            )
        except Exception as e:
            print(f"Cleanup music_search_jobs by age failed: {e}")

        try:
            cursor.execute("SELECT job_id FROM music_search_jobs ORDER BY COALESCE(updated_at, created_at, 0) DESC")
            rows = cursor.fetchall()
            if len(rows) > keep_latest:
                stale_ids = [row['job_id'] for row in rows[keep_latest:]]
                cursor.executemany("DELETE FROM music_search_jobs WHERE job_id = ?", [(job_id,) for job_id in stale_ids])
        except Exception as e:
            print(f"Cleanup music_search_jobs by count failed: {e}")

    def cleanup_music_search_jobs(self, max_age_seconds: int = 86400, keep_latest: int = 300):
        conn = self._get_conn()
        cursor = conn.cursor()
        self._cleanup_music_search_jobs(cursor, max_age_seconds=max_age_seconds, keep_latest=keep_latest)
        conn.commit()
        conn.close()

    def create_music_search_job(self, job_id: str, mode: str, keyword: str, task_id: str = None) -> None:
        now = time.time()
        conn = self._get_conn()
        cursor = conn.cursor()
        self._cleanup_music_search_jobs(cursor, max_age_seconds=24 * 3600, keep_latest=300)
        cursor.execute(
            '''
            INSERT OR REPLACE INTO music_search_jobs
            (job_id, task_id, mode, keyword, status, message, progress, total_sources, completed_sources, records_json, full_records_json, cancel_requested, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'waiting', '排队中...', 0, 0, 0, '[]', '{}', 0, ?, ?)
            ''',
            (job_id, task_id, mode, keyword, now, now),
        )
        conn.commit()
        conn.close()

    def update_music_search_job(self, job_id: str, **fields) -> bool:
        if not fields:
            return False
        conn = self._get_conn()
        cursor = conn.cursor()
        updates = []
        params = []
        for key, val in fields.items():
            updates.append(f"{key} = ?")
            params.append(val)
        updates.append("updated_at = ?")
        params.append(time.time())
        params.append(job_id)
        cursor.execute(f"UPDATE music_search_jobs SET {', '.join(updates)} WHERE job_id = ?", params)
        conn.commit()
        changed = cursor.rowcount > 0
        conn.close()
        return changed

    def set_music_search_task_id(self, job_id: str, task_id: str) -> bool:
        return self.update_music_search_job(job_id, task_id=task_id)

    def request_cancel_music_search_job(self, job_id: str) -> bool:
        return self.update_music_search_job(job_id, cancel_requested=1, status='cancelled', message='已取消')

    def is_music_search_cancel_requested(self, job_id: str) -> bool:
        item = self.get_music_search_job(job_id)
        if not item:
            return True
        return bool(item.get('cancel_requested'))

    def get_music_search_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM music_search_jobs WHERE job_id = ?", (job_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        item = dict(row)
        item['cancel_requested'] = bool(item.get('cancel_requested'))
        return item

    def find_stale_music_search_jobs(self, waiting_seconds: int = 20, running_seconds: int = 1800) -> List[Dict[str, Any]]:
        now = time.time()
        waiting_before = now - max(1, int(waiting_seconds))
        running_before = now - max(1, int(running_seconds))
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT * FROM music_search_jobs
            WHERE
                (status = 'waiting' AND COALESCE(updated_at, created_at, 0) < ?)
                OR
                (status = 'running' AND COALESCE(updated_at, created_at, 0) < ?)
            ORDER BY COALESCE(updated_at, created_at, 0) ASC
            ''',
            (waiting_before, running_before),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def mark_music_search_jobs_cancelled(self, job_ids: List[str], message: str = 'stale-cancelled'):
        if not job_ids:
            return
        conn = self._get_conn()
        cursor = conn.cursor()
        now = time.time()
        cursor.executemany(
            '''
            UPDATE music_search_jobs
            SET cancel_requested = 1,
                status = 'cancelled',
                message = ?,
                updated_at = ?
            WHERE job_id = ?
            ''',
            [(message, now, str(job_id)) for job_id in job_ids],
        )
        conn.commit()
        conn.close()

    def count_active_music_search_jobs(self) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT COUNT(*) AS cnt
            FROM music_search_jobs
            WHERE cancel_requested = 0
              AND status IN ('waiting', 'running')
            ''',
        )
        row = cursor.fetchone()
        conn.close()
        return int((row['cnt'] if row and 'cnt' in row.keys() else 0) or 0)

    def get_music_search_job_ahead(self, job_id: str) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(created_at, 0) AS created_at FROM music_search_jobs WHERE job_id = ?", (job_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return 0
        created_at = float(row['created_at'] or 0)
        cursor.execute(
            '''
            SELECT COUNT(*) AS cnt
            FROM music_search_jobs
            WHERE cancel_requested = 0
              AND status IN ('waiting', 'running')
              AND job_id != ?
              AND COALESCE(created_at, 0) < ?
            ''',
            (job_id, created_at),
        )
        count_row = cursor.fetchone()
        conn.close()
        return int((count_row['cnt'] if count_row and 'cnt' in count_row.keys() else 0) or 0)

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

    def update_song_media(
        self,
        song_id: int,
        media_type: str = None,
        singers: str = None,
        album: str = None,
        platform: str = None,
        fmt: str = None,
        size_text: str = None,
        cover_path: str = None,
        lyric_path: str = None,
    ):
        conn = self._get_conn()
        cursor = conn.cursor()
        updates = []
        params = []
        try:
            if media_type is not None:
                updates.append("media_type = ?")
                params.append(media_type)
            if singers is not None:
                updates.append("singers = ?")
                params.append(singers)
            if album is not None:
                updates.append("album = ?")
                params.append(album)
            if platform is not None:
                updates.append("platform = ?")
                params.append(platform)
            if fmt is not None:
                updates.append("format = ?")
                params.append(fmt)
            if size_text is not None:
                updates.append("size_text = ?")
                params.append(size_text)
            if cover_path is not None:
                updates.append("cover_path = ?")
                params.append(cover_path)
            if lyric_path is not None:
                updates.append("lyric_path = ?")
                params.append(lyric_path)
            if not updates:
                return
            params.append(song_id)
            cursor.execute(f"UPDATE songs SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()
        finally:
            conn.close()

    def search_music_library(self, keyword: str, limit: int = 30) -> List[Dict[str, Any]]:
        kw = str(keyword or '').strip()
        if not kw:
            return []
        conn = self._get_conn()
        cursor = conn.cursor()
        like = f"%{kw}%"
        cursor.execute(
            '''
            SELECT * FROM music_library
            WHERE title LIKE ? OR singers LIKE ? OR album LIKE ?
            ORDER BY last_used_at DESC, created_at DESC
            LIMIT ?
            ''',
            (like, like, like, limit),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def search_mv_library(self, keyword: str, limit: int = 30) -> List[Dict[str, Any]]:
        kw = str(keyword or '').strip()
        if not kw:
            return []
        conn = self._get_conn()
        cursor = conn.cursor()
        like = f"%{kw}%"
        cursor.execute(
            '''
            SELECT * FROM library
            WHERE title LIKE ? OR video_id LIKE ? OR platform LIKE ?
            ORDER BY last_used_at DESC, created_at DESC
            LIMIT ?
            ''',
            (like, like, like, limit),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_music_library_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM music_library WHERE id = ?", (item_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def save_music_library_item(
        self,
        title: str,
        singers: str,
        album: str,
        platform: str,
        fmt: str,
        size_text: str,
        file_path: str,
        raw_audio_path: str,
        cover_path: str,
        lyric_path: str,
        unique_key: str,
    ) -> Optional[int]:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = time.time()
        try:
            cursor.execute(
                '''
                INSERT INTO music_library (title, singers, album, platform, format, size_text, file_path, raw_audio_path, cover_path, lyric_path, unique_key, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(unique_key) DO UPDATE SET
                    title=excluded.title,
                    singers=excluded.singers,
                    album=excluded.album,
                    platform=excluded.platform,
                    format=excluded.format,
                    size_text=excluded.size_text,
                    file_path=excluded.file_path,
                    raw_audio_path=excluded.raw_audio_path,
                    cover_path=excluded.cover_path,
                    lyric_path=excluded.lyric_path,
                    last_used_at=excluded.last_used_at
                ''',
                (title, singers, album, platform, fmt, size_text, file_path, raw_audio_path, cover_path, lyric_path, unique_key, now, now),
            )
            conn.commit()
            cursor.execute("SELECT id FROM music_library WHERE unique_key = ?", (unique_key,))
            row = cursor.fetchone()
            return int(row['id']) if row else None
        except Exception as e:
            print(f"Music Library Save Error: {e}")
            return None
        finally:
            conn.close()

    def touch_music_library_item(self, item_id: int):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("UPDATE music_library SET last_used_at = ? WHERE id = ?", (time.time(), item_id))
        conn.commit()
        conn.close()

    def delete_music_library_item(self, item_id: int):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM music_library WHERE id = ?", (item_id,))
        conn.commit()
        conn.close()

    def delete_mv_library_song(self, video_id: str):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM library WHERE video_id = ?", (video_id,))
        conn.commit()
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
        cursor.execute("SELECT video_path, audio_path, cover_path, lyric_path FROM songs WHERE id=?", (song_id,))
        row = cursor.fetchone()
        if row:
            for key in ['video_path', 'audio_path', 'cover_path', 'lyric_path']:
                path = row[key]
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
        
        cursor.execute("DELETE FROM songs WHERE id = ?", (song_id,))
        conn.commit()
        conn.close()
