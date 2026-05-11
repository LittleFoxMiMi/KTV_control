import json
import os
import copy
import shutil
from types import MethodType
from http.cookiejar import MozillaCookieJar
from typing import Dict, List, Optional, Union
from urllib.parse import urlparse, urljoin

import requests
from musicdl import musicdl
from musicdl.modules.sources import kugou as kugou_source
from musicdl.modules.sources import kuwo as kuwo_source
from musicdl.modules.sources import netease as netease_source
from musicdl.modules.utils import kugouutils as kugou_utils
from musicdl.modules.utils import qqutils as qq_utils
from musicdl.modules.utils import SongInfo, legalizestring, safeextractfromdict, resp2json
from musicdl.modules.utils.misc import sanitize_filepath


class MusicDLService:
    def __init__(self, music_sources: Optional[List[str]] = None):
        self.cookies_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'cookies'))
        self.musicdl_cache_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'Temp', 'musicdl_outputs'))
        self.legacy_musicdl_outputs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'musicdl_outputs'))
        os.makedirs(self.musicdl_cache_dir, exist_ok=True)
        self.default_sources = music_sources or [
            'JBSouMusicClient',
            'QQMusicClient',
            'KuwoMusicClient',
            'KugouMusicClient',
            'NeteaseMusicClient',
        ]
        self.init_music_clients_cfg = {
            'JBSouMusicClient': {'search_size_per_source': 10, 'work_dir': self.musicdl_cache_dir},
            'QQMusicClient': {'search_size_per_source': 5, 'work_dir': self.musicdl_cache_dir},
            'NeteaseMusicClient': {'search_size_per_source': 5, 'work_dir': self.musicdl_cache_dir},
            'KuwoMusicClient': {'search_size_per_source': 5, 'work_dir': self.musicdl_cache_dir},
            'KugouMusicClient': {'search_size_per_source': 5, 'work_dir': self.musicdl_cache_dir},
        }
        self.requests_overrides = {
            'QQMusicClient': {'cookies': {'quality_policy': 'mp3_only'}},
            'KuwoMusicClient': {'cookies': {'quality_policy': 'mp3_only'}},
            'KugouMusicClient': {'cookies': {'quality_policy': 'mp3_only'}},
        }
        self.netease_quality_levels = ['standard']
        self.qq_sorted_qualities = [('M500', '.mp3')]
        self.kuwo_music_qualities = [(320, 'mp3')]
        self.kuwo_enc_music_qualities = [(320, '320kmp3'), (192, '192kmp3'), (128, '128kmp3')]
        self.kugou_qualities = ('320', '128')
        self.global_cookies: Optional[Union[str, Dict[str, str]]] = None
        self.quark_cookie_clients = [
            'YinyuedaoMusicClient',
            'GequbaoMusicClient',
            'MituMusicClient',
            'BuguyyMusicClient',
        ]
        self.last_search_results: Dict = {}
        self.last_music_records: Dict[str, Dict] = {}
        self.music_client: Optional[musicdl.MusicClient] = None

    @staticmethod
    def _cookiejar_to_dict(cookie_jar: requests.cookies.RequestsCookieJar) -> Dict[str, str]:
        cookies = {}
        for cookie in cookie_jar:
            cookies[cookie.name] = cookie.value
        return cookies

    def set_global_cookies(self, cookies_with_str_or_dict_format: Union[str, Dict[str, str]]) -> None:
        if isinstance(cookies_with_str_or_dict_format, str):
            self.global_cookies = cookies_with_str_or_dict_format
        elif isinstance(cookies_with_str_or_dict_format, dict):
            self.global_cookies = dict(cookies_with_str_or_dict_format)
        else:
            raise ValueError('global cookies must be str or dict format')

    def set_global_cookies_from_file(self, cookies_file: str) -> None:
        cookie_jar = self._load_cookies_from_file(cookies_file)
        self.set_global_cookies(self._cookiejar_to_dict(cookie_jar))

    def _apply_global_cookies_to_init_cfg(self) -> None:
        if not self.global_cookies:
            return
        for client_name in self.quark_cookie_clients:
            if client_name not in self.init_music_clients_cfg:
                self.init_music_clients_cfg[client_name] = {}
            client_cfg = self.init_music_clients_cfg[client_name]
            quark_cfg = client_cfg.get('quark_parser_config')
            if not isinstance(quark_cfg, dict):
                quark_cfg = {}
            quark_cfg['cookies'] = self.global_cookies
            client_cfg['quark_parser_config'] = quark_cfg

    def _create_music_client(self, sources: List[str], requests_overrides: Dict, fast_search: bool) -> musicdl.MusicClient:
        self._apply_global_cookies_to_init_cfg()
        client = musicdl.MusicClient(
            music_sources=sources,
            init_music_clients_cfg=self.init_music_clients_cfg,
            requests_overrides=requests_overrides,
        )
        self._enable_jbsou_light_search(client, enabled=fast_search)
        if fast_search:
            for per_client in client.music_clients.values():
                tester = getattr(per_client, 'audio_link_tester', None)
                if tester is None:
                    continue
                try:
                    tester.test = lambda url, request_overrides=None: {'ok': True}
                    tester.probe = lambda url, request_overrides=None: {'ext': 'mp3', 'file_size': 'NULL'}
                except Exception:
                    continue
        return client

    def _enable_jbsou_light_search(self, client: musicdl.MusicClient, enabled: bool = True) -> None:
        if not enabled:
            return
        jbsou_client = getattr(client, 'music_clients', {}).get('JBSouMusicClient')
        if jbsou_client is None:
            return
        if getattr(jbsou_client, '_ktv_light_search_enabled', False):
            return

        def _light_search(bound_self, keyword: str = '', search_url: dict = None, request_overrides: dict = None, song_infos: list = None, progress=None, progress_id: int = 0):
            request_overrides = request_overrides or {}
            song_infos = song_infos if isinstance(song_infos, list) else []
            base_url = 'https://www.jbsou.cn/'
            source = ((search_url or {}).get('data') or {}).get('type') or ''
            if source in ['netease', 'qq']:
                return song_infos
            try:
                resp = bound_self.post(**(search_url or {}), **request_overrides)
                resp.raise_for_status()
                parsed_payload = resp2json(resp) or {}
                search_results = parsed_payload.get('data') or []
                debug_page = ((search_url or {}).get('data') or {}).get('page', '-')
                print(f"[JBSOU RAW] source={source} keyword={keyword} page={debug_page} count={len(search_results) if isinstance(search_results, list) else 0}")
                try:
                    print(f"[JBSOU RAW DATA] {json.dumps(search_results, ensure_ascii=False)}")
                except Exception:
                    print(f"[JBSOU RAW DATA] {search_results}")
                for search_result in search_results:
                    if not isinstance(search_result, dict):
                        continue
                    song_id = search_result.get('songid')
                    download_path = search_result.get('url')
                    if not song_id or not download_path:
                        continue
                    download_url = urljoin(base_url, str(download_path))
                    cover_url = urljoin(base_url, str(search_result.get('cover', '') or ''))
                    ext = str(download_url).split('?')[0].split('.')[-1] if '.' in str(download_url).split('?')[0] else 'mp3'

                    song_info = SongInfo(
                        raw_data={'search': search_result, 'download': {}, 'lyric': {}},
                        source=bound_self.source,
                        root_source=source,
                        song_name=legalizestring(safeextractfromdict(search_result, ['name'], None)),
                        singers=legalizestring(str(safeextractfromdict(search_result, ['artist'], '')).replace('/', ', ')),
                        album=legalizestring(search_result.get('album')),
                        ext=ext or 'mp3',
                        file_size='NULL',
                        identifier=str(song_id),
                        duration='-:-:-',
                        lyric='NULL',
                        cover_url=cover_url,
                        download_url=download_url,
                        download_url_status={'ok': True},
                    )
                    song_infos.append(song_info)
                    if bound_self.strict_limit_search_size_per_page and len(song_infos) >= bound_self.search_size_per_page:
                        break
                if progress is not None:
                    progress.update(progress_id, description=f"{bound_self.source}.search >>> {search_url} (Success, lightweight)")
            except Exception as err:
                if progress is not None:
                    progress.update(progress_id, description=f"{bound_self.source}.search >>> {search_url} (Error: {err})")
            return song_infos

        jbsou_client._search = MethodType(_light_search, jbsou_client)
        jbsou_client._ktv_light_search_enabled = True

    def _apply_runtime_quality_policy(self) -> None:
        try:
            netease_source.MUSIC_QUALITIES = list(self.netease_quality_levels)
        except Exception:
            pass
        try:
            qq_utils.SongFileType.SORTED_QUALITIES.value = list(self.qq_sorted_qualities)
        except Exception:
            pass
        try:
            kuwo_source.KuwoMusicClient.MUSIC_QUALITIES = list(self.kuwo_music_qualities)
            kuwo_source.KuwoMusicClient.ENC_MUSIC_QUALITIES = list(self.kuwo_enc_music_qualities)
        except Exception:
            pass
        try:
            kugou_utils.MUSIC_QUALITIES = tuple(self.kugou_qualities)
            kugou_source.MUSIC_QUALITIES = tuple(self.kugou_qualities)
        except Exception:
            pass

    def _build_search_requests_overrides(self, source_cookies_files: Optional[Dict[str, str]] = None) -> Dict:
        merged = copy.deepcopy(self.requests_overrides)
        source_cookies_files = source_cookies_files or {}
        for source, cookies_file in source_cookies_files.items():
            if not cookies_file or not os.path.isfile(cookies_file):
                continue
            if source not in merged:
                merged[source] = {}
            try:
                merged[source]['cookies'] = self._load_cookies_from_file(cookies_file)
            except Exception:
                pass
        return merged

    def _build_source_cookie_files_from_map(self, source_cookie_file_map: Optional[Dict[str, str]]) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for source, file_name in (source_cookie_file_map or {}).items():
            if not source or not file_name:
                continue
            abs_path = os.path.join(self.cookies_dir, file_name)
            if os.path.isfile(abs_path):
                mapping[source] = abs_path
        return mapping

    def _strip_netease_cookies_for_search(self, requests_overrides: Dict) -> Dict:
        result = copy.deepcopy(requests_overrides)
        if 'NeteaseMusicClient' in result and isinstance(result['NeteaseMusicClient'], dict):
            result['NeteaseMusicClient'].pop('cookies', None)
            if not result['NeteaseMusicClient']:
                result.pop('NeteaseMusicClient', None)
        return result

    @staticmethod
    def _cover_penalty(song_name: str) -> int:
        text = str(song_name or '').lower()
        cover_keywords = ['翻唱', 'cover', 'live', '现场', '伴奏', 'dj', 'remix', '串烧', '版']
        return 1 if any(keyword in text for keyword in cover_keywords) else 0

    def _sort_records(self, records: List[Dict], sort_mode: str = 'default') -> List[Dict]:
        if sort_mode != 'original_first':
            return records

        source_priority = {
            'JBSouMusicClient': 0,
            'QQMusicClient': 0,
            'NeteaseMusicClient': 1,
            'KuwoMusicClient': 2,
            'KugouMusicClient': 3,
        }
        return sorted(
            records,
            key=lambda x: (
                self._cover_penalty(x.get('song_name', '')),
                source_priority.get(x.get('source', ''), 99),
            ),
        )

    def search_music(
        self,
        keyword: str,
        music_sources: Optional[List[str]] = None,
        source_cookies_files: Optional[Dict[str, str]] = None,
        source_cookie_file_map: Optional[Dict[str, str]] = None,
        all_cookies_file: Optional[str] = None,
        fast_search: bool = True,
        enable_qq_separate_search: bool = False,
        sort_mode: str = 'default',
    ) -> Dict:
        if all_cookies_file and os.path.isfile(all_cookies_file):
            self.set_global_cookies_from_file(all_cookies_file)
        sources = music_sources or self.default_sources
        self._apply_runtime_quality_policy()
        mapped_source_cookie_files = self._build_source_cookie_files_from_map(source_cookie_file_map)
        source_cookies_files = source_cookies_files or {}
        source_cookies_files.update(mapped_source_cookie_files)
        requests_overrides = self._build_search_requests_overrides(source_cookies_files)
        requests_overrides = self._strip_netease_cookies_for_search(requests_overrides)
        effective_sources = [source for source in sources if source != 'QQMusicClient']

        if enable_qq_separate_search:
            effective_sources = ['QQMusicClient']
            self.music_client = self._create_music_client(effective_sources, requests_overrides, fast_search)
            self.last_search_results = self.music_client.search(keyword=keyword)
        else:
            self.music_client = self._create_music_client(effective_sources, requests_overrides, fast_search)
            self.last_search_results = self.music_client.search(keyword=keyword)

        self.last_music_records = {}
        normalized_records = []
        for per_source_search_results in self.last_search_results.values():
            for per_source_search_result in per_source_search_results:
                ext = self._clean_nullish(per_source_search_result.get('ext', 'mp3'), default='mp3') or 'mp3'
                normalized = {
                    'id': -1,
                    'singers': self._clean_nullish(per_source_search_result.get('singers', ''), default=''),
                    'song_name': self._clean_nullish(per_source_search_result.get('song_name', ''), default=''),
                    'file_size': self._clean_nullish(per_source_search_result.get('file_size', ''), default=''),
                    'duration': self._clean_nullish(per_source_search_result.get('duration', ''), default=''),
                    'album': self._clean_nullish(per_source_search_result.get('album', ''), default=''),
                    'source': per_source_search_result.get('source', ''),
                    'download_url': per_source_search_result.get('download_url', ''),
                    'cover_url': per_source_search_result.get('cover_url', ''),
                    'lyric': self._clean_nullish(per_source_search_result.get('lyric', ''), default=''),
                    'raw_search': (((per_source_search_result.get('raw_data') or {}) if isinstance(per_source_search_result.get('raw_data'), dict) else {}).get('search') or {}),
                    'work_dir': per_source_search_result.get('work_dir', ''),
                    'ext': ext,
                }
                normalized_records.append(normalized)

        normalized_records = self._sort_records(normalized_records, sort_mode=sort_mode)

        public_records = []
        for new_id, normalized in enumerate(normalized_records):
            normalized['id'] = new_id
            self.last_music_records[str(new_id)] = normalized
            public_records.append(
                {
                    'id': normalized['id'],
                    'singers': normalized['singers'],
                    'song_name': normalized['song_name'],
                    'file_size': normalized['file_size'],
                    'duration': normalized['duration'],
                    'album': normalized['album'],
                    'source': normalized['source'],
                    'ext': normalized['ext'],
                }
            )

        return {
            'keyword': keyword,
            'sources': effective_sources,
            'total': len(public_records),
            'records': public_records,
            'sort_mode': sort_mode,
            'qq_separate_search': enable_qq_separate_search,
        }

    def _load_cookies_from_file(self, cookies_file: str) -> requests.cookies.RequestsCookieJar:
        if not os.path.isfile(cookies_file):
            raise FileNotFoundError(f'cookies file not found: {cookies_file}')

        cookie_jar = requests.cookies.RequestsCookieJar()

        try:
            mozilla_jar = MozillaCookieJar(cookies_file)
            mozilla_jar.load(ignore_discard=True, ignore_expires=True)
            for c in mozilla_jar:
                cookie_jar.set(c.name, c.value, domain=c.domain, path=c.path)
            return cookie_jar
        except Exception:
            pass

        with open(cookies_file, 'r', encoding='utf-8') as fp:
            for line in fp:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                name, value = line.split('=', 1)
                cookie_jar.set(name.strip(), value.strip())

        return cookie_jar
    @staticmethod
    def _normalize_text(text: str) -> str:
        return ''.join(str(text or '').lower().split())

    @staticmethod
    def _clean_nullish(value, default: str = '') -> str:
        text = str(value if value is not None else '').strip()
        if text.lower() in {'null', 'none', 'nan'}:
            return default
        return text

    @staticmethod
    def _is_mp3_ext(ext: str) -> bool:
        return str(ext or '').lower() == 'mp3'

    @staticmethod
    def _parse_filesize_mb(file_size: str) -> float:
        text = str(file_size or '').strip().upper()
        if not text:
            return 999999.0
        try:
            if text.endswith('GB'):
                return float(text[:-2].strip()) * 1024
            if text.endswith('MB'):
                return float(text[:-2].strip())
            if text.endswith('KB'):
                return float(text[:-2].strip()) / 1024
            return float(text)
        except Exception:
            return 999999.0

    @staticmethod
    def _is_suspicious_cover_url(url: str) -> bool:
        lower_url = str(url or '').lower()
        suspicious_tokens = ['mvpic', '/mv/', 'snapshot', 'screenshot', 'video', 'covermv']
        return any(token in lower_url for token in suspicious_tokens)

    @staticmethod
    def _normalize_cover_url(url: str) -> str:
        if not url:
            return ''
        if url.startswith('//'):
            return 'https:' + url
        return url

    def _resolve_lyric_value(self, song_info: Dict) -> str:
        lyric_value = self._clean_nullish(song_info.get('lyric', ''), default='')
        if lyric_value:
            return lyric_value

        raw_search = song_info.get('raw_search') or {}
        if not isinstance(raw_search, dict):
            raw_search = {}
        raw_lrc = self._clean_nullish(raw_search.get('lrc', ''), default='')
        if not raw_lrc:
            return ''
        if raw_lrc.startswith('//'):
            return 'https:' + raw_lrc
        if raw_lrc.startswith('http://') or raw_lrc.startswith('https://'):
            return raw_lrc
        return urljoin('https://www.jbsou.cn/', raw_lrc)

    def _resolve_cover_url(self, song_info: Dict) -> str:
        source = song_info.get('source', '')
        raw_search = song_info.get('raw_search') or {}
        if not isinstance(raw_search, dict):
            raw_search = {}

        candidates: List[str] = []
        if song_info.get('cover_url'):
            candidates.append(str(song_info.get('cover_url')))

        if source == 'KuwoMusicClient':
            kuwo_candidates = [
                raw_search.get('web_albumpic_short'),
                raw_search.get('albumpic'),
                raw_search.get('ALBUMPIC'),
                raw_search.get('hts_img'),
            ]
            for item in kuwo_candidates:
                if not item:
                    continue
                item = str(item)
                if item.startswith('http') or item.startswith('//'):
                    candidates.append(item)
                else:
                    candidates.append('https://img4.kuwo.cn/star/albumcover/300/' + item.lstrip('/'))

        if source == 'QQMusicClient':
            album_mid = ''
            album_obj = raw_search.get('album')
            if isinstance(album_obj, dict):
                album_mid = str(album_obj.get('mid') or '')
            album_mid = album_mid or str(raw_search.get('albummid') or '')
            if album_mid:
                candidates.append(f'https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg')

        if source == 'NeteaseMusicClient':
            al = raw_search.get('al')
            if isinstance(al, dict) and al.get('picUrl'):
                candidates.append(str(al.get('picUrl')))

        for candidate in candidates:
            candidate = self._normalize_cover_url(candidate)
            if not candidate:
                continue
            parsed = urlparse(candidate)
            if parsed.scheme not in {'http', 'https'}:
                continue
            if self._is_suspicious_cover_url(candidate):
                continue
            return candidate

        return ''

    def _download_binary_file(
        self,
        session: requests.Session,
        url: str,
        save_path: str,
        headers: Optional[dict] = None,
    ) -> str:
        with session.get(url, headers=headers or {}, stream=True, verify=False, timeout=30) as resp:
            resp.raise_for_status()
            with open(save_path, 'wb') as fp:
                for chunk in resp.iter_content(chunk_size=1024):
                    if chunk:
                        fp.write(chunk)
        return save_path

    def _fix_file_extension(self, file_path: str, current_ext: str, song_name: str, save_dir: str):
        try:
            import filetype
            kind = filetype.guess(file_path)
            if kind is not None:
                real_ext = kind.extension
                if current_ext in ['php'] or real_ext in ['mp3', 'flac', 'wav', 'm4a', 'aac', 'ogg'] and real_ext != current_ext:
                    new_file_path = sanitize_filepath(os.path.join(save_dir, f'{song_name}.{real_ext}'))
                    if os.path.exists(new_file_path):
                        os.remove(new_file_path)
                    os.rename(file_path, new_file_path)
                    return new_file_path, real_ext, None
        except Exception as e:
            return file_path, current_ext, f'failed to resolve real extension: {e}'
        return file_path, current_ext, None

    def _cleanup_musicdl_output_dirs(self) -> None:
        for output_dir in [self.musicdl_cache_dir, self.legacy_musicdl_outputs_dir]:
            if not output_dir or not os.path.isdir(output_dir):
                continue
            for name in os.listdir(output_dir):
                target = os.path.join(output_dir, name)
                try:
                    if os.path.isdir(target):
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        os.remove(target)
                except Exception:
                    continue

    def _pick_mp3_candidate(self, current_song_info: Dict) -> Optional[Dict]:
        current_song = self._normalize_text(current_song_info.get('song_name', ''))
        current_singers = self._normalize_text(current_song_info.get('singers', ''))

        source_priority = {
            'QQMusicClient': 0,
            'KuwoMusicClient': 1,
            'KugouMusicClient': 2,
            'NeteaseMusicClient': 3,
        }

        candidates = []
        for record in self.last_music_records.values():
            if not self._is_mp3_ext(record.get('ext', '')):
                continue
            if not record.get('download_url'):
                continue

            song_name = self._normalize_text(record.get('song_name', ''))
            singers = self._normalize_text(record.get('singers', ''))
            if song_name != current_song:
                continue
            if current_singers and singers and current_singers not in singers and singers not in current_singers:
                continue

            candidates.append(record)

        if not candidates:
            return None

        candidates.sort(
            key=lambda x: (
                source_priority.get(str(x.get('source', '')), 99),
                self._parse_filesize_mb(x.get('file_size', '')),
            )
        )
        return candidates[0]

    def download_music(
        self,
        song_id: int,
        save_dir: str,
        cookies_file: Optional[str] = None,
        prefer_mp3: bool = True,
        allow_non_mp3_fallback: bool = False,
        download_cover: bool = True,
        download_lyric: bool = True,
    ) -> Dict:
        try:
            if self.music_client is None or not self.last_music_records:
                raise RuntimeError('no search result available, run search_music first')

            song_info = self.last_music_records.get(str(song_id))
            if song_info is None:
                raise ValueError(f'invalid song id: {song_id}')

            if prefer_mp3 and not self._is_mp3_ext(song_info.get('ext', '')):
                mp3_song_info = self._pick_mp3_candidate(song_info)
                if mp3_song_info is not None:
                    song_info = mp3_song_info
                elif not allow_non_mp3_fallback:
                    raise RuntimeError('no mp3 candidate found for this song, skipped to enforce mp3-only policy')

            download_url = song_info.get('download_url')
            if not download_url:
                raise ValueError(f'song id {song_id} has no download url')

            source = song_info.get('source', '')
            headers = {}
            if self.music_client and source in getattr(self.music_client, 'music_clients', {}):
                client_obj = self.music_client.music_clients[source]
                headers = getattr(client_obj, 'default_download_headers', getattr(client_obj, 'default_headers', {}))
            if not headers:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36'}

            session = requests.Session()
            if cookies_file:
                session.cookies.update(self._load_cookies_from_file(cookies_file))

            os.makedirs(save_dir, exist_ok=True)
            song_name = song_info.get('song_name', f'song_{song_id}')
            ext = song_info.get('ext', 'mp3')
            file_path = sanitize_filepath(os.path.join(save_dir, f'{song_name}.{ext}'))
            warnings: List[str] = []

            self._download_binary_file(session, download_url, file_path, headers=headers)
            file_path, ext, fix_warn = self._fix_file_extension(file_path, ext, song_name, save_dir)
            if fix_warn:
                warnings.append(fix_warn)

            cover_path = None
            cover_url = self._resolve_cover_url(song_info)
            if download_cover and cover_url.startswith('http'):
                cover_path = sanitize_filepath(os.path.join(save_dir, f'{song_name}.jpg'))
                try:
                    self._download_binary_file(session, cover_url, cover_path, headers=headers)
                except Exception:
                    cover_path = None
                    warnings.append('cover download failed')

            lyric_path = None
            lyric_value = self._resolve_lyric_value(song_info)
            if download_lyric and lyric_value:
                lyric_path = sanitize_filepath(os.path.join(save_dir, f'{song_name}.lrc'))
                try:
                    lyric_text = ''
                    if isinstance(lyric_value, str) and lyric_value.startswith('http'):
                        with session.get(lyric_value, headers=headers, timeout=30, verify=False) as resp:
                            resp.raise_for_status()
                            lyric_text = resp.text
                    else:
                        lyric_text = str(lyric_value)
                    with open(lyric_path, 'w', encoding='utf-8') as fp:
                        fp.write(lyric_text)
                except Exception:
                    lyric_path = None
                    warnings.append('lyric download failed')

            return {
                'song_id': song_id,
                'song_name': song_name,
                'singers': song_info.get('singers', ''),
                'source': source,
                'file_path': file_path,
                'ext': ext,
                'prefer_mp3': prefer_mp3,
                'allow_non_mp3_fallback': allow_non_mp3_fallback,
                'cover_path': cover_path,
                'lyric_path': lyric_path,
                'warnings': warnings,
                'status': 'success',
            }
        finally:
            self._cleanup_musicdl_output_dirs()

    def download_music_record(
        self,
        song_record: Dict,
        save_dir: str,
        cookies_file: Optional[str] = None,
        download_cover: bool = True,
        download_lyric: bool = True,
    ) -> Dict:
        try:
            if not isinstance(song_record, dict):
                raise ValueError('song_record must be dict')

            download_url = song_record.get('download_url')
            if not download_url:
                raise ValueError('song record has no download url')

            source = song_record.get('source', '')
            headers = {}
            if self.music_client and source in getattr(self.music_client, 'music_clients', {}):
                client_obj = self.music_client.music_clients[source]
                headers = getattr(client_obj, 'default_download_headers', getattr(client_obj, 'default_headers', {}))
            if not headers:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36'}

            session = requests.Session()
            if cookies_file:
                session.cookies.update(self._load_cookies_from_file(cookies_file))

            os.makedirs(save_dir, exist_ok=True)
            song_name = song_record.get('song_name') or 'song'
            ext = song_record.get('ext') or 'mp3'
            file_path = sanitize_filepath(os.path.join(save_dir, f'{song_name}.{ext}'))
            warnings: List[str] = []

            self._download_binary_file(session, download_url, file_path, headers=headers)
            file_path, ext, fix_warn = self._fix_file_extension(file_path, ext, song_name, save_dir)
            if fix_warn:
                warnings.append(fix_warn)

            cover_path = None
            cover_url = self._resolve_cover_url(song_record)
            if download_cover and cover_url.startswith('http'):
                cover_path = sanitize_filepath(os.path.join(save_dir, f'{song_name}.jpg'))
                try:
                    self._download_binary_file(session, cover_url, cover_path, headers=headers)
                except Exception:
                    cover_path = None
                    warnings.append('cover download failed')

            lyric_path = None
            lyric_value = self._resolve_lyric_value(song_record)
            if download_lyric and lyric_value:
                lyric_path = sanitize_filepath(os.path.join(save_dir, f'{song_name}.lrc'))
                try:
                    lyric_text = ''
                    if isinstance(lyric_value, str) and lyric_value.startswith('http'):
                        with session.get(lyric_value, headers=headers, timeout=30, verify=False) as resp:
                            resp.raise_for_status()
                            lyric_text = resp.text
                    else:
                        lyric_text = str(lyric_value)
                    with open(lyric_path, 'w', encoding='utf-8') as fp:
                        fp.write(lyric_text)
                except Exception:
                    lyric_path = None
                    warnings.append('lyric download failed')

            return {
                'song_name': song_name,
                'singers': song_record.get('singers', ''),
                'album': song_record.get('album', ''),
                'source': source,
                'file_path': file_path,
                'ext': ext,
                'file_size': song_record.get('file_size', ''),
                'cover_path': cover_path,
                'lyric_path': lyric_path,
                'warnings': warnings,
                'status': 'success',
            }
        finally:
            self._cleanup_musicdl_output_dirs()


if __name__ == '__main__':
    service = MusicDLService()

    config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'config.json'))
    source_cookie_file_map = {
        'QQMusicClient': 'QQ.txt',
        'NeteaseMusicClient': '163.txt',
        'KuwoMusicClient': '',
        'KugouMusicClient': '',
    }
    if os.path.isfile(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as fp:
                cfg = json.load(fp)
            from_cfg = ((cfg.get('music') or {}).get('cookie_file_map') or {})
            if isinstance(from_cfg, dict) and from_cfg:
                source_cookie_file_map = {str(k): str(v) for k, v in from_cfg.items()}
        except Exception:
            pass

    test_keyword = '春雪'
    print(f'[TEST] search keyword: {test_keyword}')
    search_result = service.search_music(
        test_keyword,
        source_cookie_file_map=source_cookie_file_map,
        fast_search=True,
        enable_qq_separate_search=False,
        sort_mode='original_first',
    )
    print('[TEST] search result json:')
    print(json.dumps(search_result, ensure_ascii=False, indent=2))

    if search_result['total'] > 0:
        if input('run download test with the first search result? (y/n) ').lower() != 'y':
            print('[TEST] download test skipped by user')
            exit(0)
        test_song_id = input(f'input song id to download (0-{search_result["total"] - 1}): ')
        test_save_dir = os.path.join(os.path.dirname(__file__), '..', 'test')
        test_cookies_file = os.path.join(os.path.dirname(__file__), '..', 'cookies', '163.txt')
        print(f'[TEST] try downloading song_id={test_song_id}')
        try:
            download_result = service.download_music(
                song_id=test_song_id,
                save_dir=test_save_dir,
                cookies_file=test_cookies_file if os.path.isfile(test_cookies_file) else None,
                prefer_mp3=True,
                allow_non_mp3_fallback=False,
                download_cover=True,
                download_lyric=True,
            )
            print('[TEST] download result json:')
            print(json.dumps(download_result, ensure_ascii=False, indent=2))
        except Exception as exc:
            print(f'[TEST] download failed: {exc}')
    else:
        print('[TEST] no songs found, skip download test')
