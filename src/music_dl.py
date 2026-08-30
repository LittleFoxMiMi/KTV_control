import json
import os
import re
import shutil
from types import MethodType
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
from musicdl import musicdl
from musicdl.modules.sources import kugou as kugou_source
from musicdl.modules.sources import kuwo as kuwo_source
from musicdl.modules.sources import netease as netease_source
from musicdl.modules.utils import kugouutils as kugou_utils
from musicdl.modules.utils import qqutils as qq_utils
from musicdl.modules.utils import SongInfo, legalizestring, resp2json
from musicdl.modules.utils.misc import sanitize_filepath


DEFERRED_DOWNLOAD_PREFIX = 'https://musicsearch.invalid/'


class MusicDLService:
    def __init__(self, music_sources: Optional[List[str]] = None):
        self.musicdl_cache_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'Temp', 'musicdl_outputs'))
        self.legacy_musicdl_outputs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'musicdl_outputs'))
        os.makedirs(self.musicdl_cache_dir, exist_ok=True)
        self.default_sources = music_sources or [
            'KuwoMusicClient',
            'NeteaseMusicClient',
            'QQMusicClient',
        ]
        self.init_music_clients_cfg = {
            'KuwoMusicClient': {'search_size_per_source': 5, 'work_dir': self.musicdl_cache_dir},
            'NeteaseMusicClient': {'search_size_per_source': 5, 'work_dir': self.musicdl_cache_dir},
            'QQMusicClient': {'search_size_per_source': 5, 'work_dir': self.musicdl_cache_dir},
            'KugouMusicClient': {'search_size_per_source': 5, 'work_dir': self.musicdl_cache_dir},
        }
        self.netease_quality_levels = ['standard']
        self.qq_sorted_qualities = [('M500', '.mp3')]
        self.kuwo_music_qualities = [(320, 'mp3')]
        self.kuwo_enc_music_qualities = [(320, '320kmp3'), (192, '192kmp3'), (128, '128kmp3')]
        self.kugou_qualities = ('320', '128')
        self.last_search_results: Dict = {}
        self.last_music_records: Dict[str, Dict] = {}
        self.music_client: Optional[musicdl.MusicClient] = None

    def _create_music_client(self, sources: List[str], requests_overrides: Optional[Dict] = None, fast_search: bool = True) -> musicdl.MusicClient:
        client = musicdl.MusicClient(
            music_sources=sources,
            init_music_clients_cfg=self.init_music_clients_cfg,
            requests_overrides=requests_overrides or {},
        )
        if fast_search:
            self._enable_lightweight_search(client)
            for per_client in client.music_clients.values():
                tester = getattr(per_client, 'audio_link_tester', None)
                if tester is None:
                    continue
                try:
                    # musicdl 2.13.x calls test(..., renew_session=...) and
                    # expects the complete link metadata, not just ``ok``.
                    tester.test = (
                        lambda url, request_overrides=None, renew_session=True, **kwargs: {
                            'ok': True,
                            'ext': 'mp3',
                            'file_size': 'NULL',
                            'file_size_bytes': 0,
                            'download_url': url,
                            'original_download_url': url,
                            'download_url_status': {'ok': True},
                        }
                    )
                    # Keep compatibility with pre-2.13 musicdl releases that
                    # exposed a separate probe method.
                    if hasattr(tester, 'probe'):
                        tester.probe = lambda url, request_overrides=None, **kwargs: {
                            'ext': 'mp3', 'file_size': 'NULL', 'file_size_bytes': 0,
                        }
                except Exception:
                    continue
        return client

    @staticmethod
    def _lightweight_search_result_list(source: str, payload: dict) -> list:
        if not isinstance(payload, dict):
            return []
        if source == 'QQMusicClient':
            return (((payload.get('music.search.SearchCgiService.DoSearchForQQMusicMobile') or {}).get('data') or {})
                    .get('body') or {}).get('item_song') or []
        if source == 'NeteaseMusicClient':
            return ((payload.get('result') or {}).get('songs') or [])
        if source == 'KuwoMusicClient':
            return payload.get('abslist') or []
        return []

    @staticmethod
    def _lightweight_song_info(source_client, source: str, search_result: dict) -> SongInfo:
        if source == 'QQMusicClient':
            song_id = search_result.get('mid') or search_result.get('songmid') or search_result.get('id')
            song_name = search_result.get('title') or search_result.get('songname') or ''
            singers = ', '.join(
                str(item.get('name')) for item in (search_result.get('singer') or [])
                if isinstance(item, dict) and item.get('name')
            )
            album_obj = search_result.get('album') if isinstance(search_result.get('album'), dict) else {}
            album = album_obj.get('title') or search_result.get('albumname') or ''
        elif source == 'NeteaseMusicClient':
            song_id = search_result.get('id')
            song_name = search_result.get('name') or ''
            singers = ', '.join(
                str(item.get('name')) for item in (search_result.get('ar') or [])
                if isinstance(item, dict) and item.get('name')
            )
            album_obj = search_result.get('al') if isinstance(search_result.get('al'), dict) else {}
            album = album_obj.get('name') or ''
        else:
            song_id = search_result.get('MUSICRID') or search_result.get('musicrid') or search_result.get('id')
            song_name = search_result.get('SONGNAME') or search_result.get('songName') or search_result.get('name') or ''
            singers = search_result.get('ARTIST') or search_result.get('artist') or ''
            album = search_result.get('ALBUM') or search_result.get('album') or ''

        identifier = str(song_id or '')
        # BaseMusicClient.search keeps only SongInfo objects with a valid URL.
        # Use a non-routable marker so the result survives normalization; the
        # download worker resolves it to a real URL only after selection.
        deferred_url = f'{DEFERRED_DOWNLOAD_PREFIX}{source}/{identifier}'
        if source == 'QQMusicClient':
            raw_keys = ('id', 'mid', 'songmid', 'title', 'songname', 'singer', 'album', 'albumname', 'interval')
        elif source == 'NeteaseMusicClient':
            raw_keys = ('id', 'name', 'ar', 'al', 'dt')
        else:
            raw_keys = (
                'MUSICRID', 'musicrid', 'SONGNAME', 'songName', 'name',
                'ARTIST', 'artist', 'ALBUM', 'album', 'DURATION', 'duration',
                'ALBUMPIC', 'albumpic', 'web_albumpic_short', 'hts_img',
                'hts_MVPIC', 'pic',
            )
        raw_search = {key: search_result.get(key) for key in raw_keys if search_result.get(key) not in (None, '')}
        return SongInfo(
            raw_data={'search': raw_search, 'download': {}, 'lyric': {}},
            source=source_client.source,
            root_source=source,
            song_name=legalizestring(song_name),
            singers=legalizestring(singers),
            album=legalizestring(album),
            ext='mp3',
            file_size='NULL',
            identifier=identifier,
            duration='-:-:-',
            lyric='',
            cover_url='',
            download_url=deferred_url,
            download_url_status={'ok': True, 'deferred': True},
        )

    def _enable_lightweight_search(self, client: musicdl.MusicClient) -> None:
        for source in ('KuwoMusicClient', 'NeteaseMusicClient', 'QQMusicClient'):
            source_client = getattr(client, 'music_clients', {}).get(source)
            if source_client is None or getattr(source_client, '_ktv_lightweight_search', False):
                continue

            def _light_search(bound_self, keyword='', search_url=None, request_overrides=None, song_infos=None, progress=None, progress_id=0, _source=source):
                request_overrides = request_overrides or {}
                song_infos = song_infos if isinstance(song_infos, list) else []
                if isinstance(search_url, str):
                    search_endpoint, search_meta = search_url, {}
                else:
                    search_meta = dict(search_url or {})
                    search_endpoint = search_meta.pop('url', '')
                    if _source == 'QQMusicClient':
                        search_meta.pop('page_no', None)
                    elif _source == 'NeteaseMusicClient':
                        search_meta.pop('page', None)
                try:
                    response = bound_self.get(search_endpoint, **search_meta, **request_overrides) if _source == 'KuwoMusicClient' else bound_self.post(search_endpoint, **search_meta, **request_overrides)
                    response.raise_for_status()
                    results = self._lightweight_search_result_list(_source, resp2json(response) or {})
                    for search_result in results:
                        if not isinstance(search_result, dict):
                            continue
                        song_infos.append(self._lightweight_song_info(bound_self, _source, search_result))
                        if bound_self.strict_limit_search_size_per_page and len(song_infos) >= bound_self.search_size_per_page:
                            break
                    if progress is not None:
                        progress.update(progress_id, description=f'{bound_self.source}.search >>> lightweight results')
                except Exception as err:
                    if progress is not None:
                        try:
                            progress.update(progress_id, description=f'{bound_self.source}.search >>> lightweight error: {err}')
                        except (KeyError, IndexError, AttributeError):
                            pass
                return song_infos

            source_client._search = MethodType(_light_search, source_client)
            source_client._ktv_lightweight_search = True

    def _apply_runtime_quality_policy(self) -> None:
        try:
            netease_source.MUSIC_QUALITIES = list(self.netease_quality_levels)
        except Exception:
            pass
        try:
            # ``SORTED_QUALITIES`` is an Enum member whose value is a mutable
            # list; mutate it in place so the quality order actually changes.
            qq_utils.SongFileType.SORTED_QUALITIES.value[:] = list(self.qq_sorted_qualities)
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

    @staticmethod
    def _cover_penalty(song_name: str) -> int:
        text = str(song_name or '').lower()
        cover_keywords = ['翻唱', 'cover', 'live', '现场', '伴奏', 'dj', 'remix', '串烧', '版']
        return 1 if any(keyword in text for keyword in cover_keywords) else 0

    def _sort_records(self, records: List[Dict], sort_mode: str = 'default') -> List[Dict]:
        if sort_mode != 'original_first':
            return records

        source_priority = {
            'KuwoMusicClient': 0,
            'NeteaseMusicClient': 1,
            'QQMusicClient': 2,
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
        fast_search: bool = True,
        enable_qq_separate_search: bool = False,
        sort_mode: str = 'default',
    ) -> Dict:
        sources = music_sources or self.default_sources
        self._apply_runtime_quality_policy()
        requests_overrides = {}
        # Only clients configured by this service are searchable. This keeps
        # removed/unknown source names from being re-enabled by callers that
        # pass an explicit ``music_sources`` list.
        effective_sources = [source for source in sources if source in self.init_music_clients_cfg]
        if not effective_sources:
            effective_sources = list(self.default_sources)

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
                    'search_keyword': keyword,
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

    def resolve_deferred_music_record(self, song_record: Dict) -> Dict:
        """Resolve a lightweight search row when the user actually selects it."""
        if not isinstance(song_record, dict):
            raise ValueError('song_record must be dict')
        deferred_url = str(song_record.get('download_url') or '')
        if not deferred_url.startswith(DEFERRED_DOWNLOAD_PREFIX):
            return song_record

        source = str(song_record.get('source') or '')
        keyword = str(song_record.get('search_keyword') or '').strip()
        if not keyword:
            keyword = ' '.join(filter(None, [str(song_record.get('song_name') or ''), str(song_record.get('singers') or '')])).strip()
        if not source or not keyword:
            raise RuntimeError('lightweight search result is missing source or title')

        raw_search = song_record.get('raw_search') if isinstance(song_record.get('raw_search'), dict) else {}
        # Apply the service's configured quality order in the resolver process
        # as well; this process starts with a fresh module on Windows.
        self._apply_runtime_quality_policy()
        client = self._create_music_client([source], fast_search=False)
        source_client = client.music_clients.get(source)
        if source_client is None:
            raise RuntimeError(f'unsupported music source: {source}')

        request_overrides = {}
        song_info_flac = source_client._parsewiththirdpartapis(
            search_result=dict(raw_search), request_overrides=request_overrides,
        )
        song_info = song_info_flac
        try:
            # The app's quality policy (standard/320K/M500) must win over a
            # lossless result returned by the fallback parser.
            lossless_quality_is_sufficient = False
            song_info = source_client._parsewithofficialapiv1(
                search_result=dict(raw_search),
                # Do not pass a third-party Hi-Res candidate here: some
                # clients compare its quality label against the configured
                # list and otherwise raise before trying the requested level.
                song_info_flac=None,
                lossless_quality_is_sufficient=lossless_quality_is_sufficient,
                request_overrides=request_overrides,
            )
        except Exception:
            pass
        selected = song_info if song_info.with_valid_download_url else song_info_flac
        if not selected.with_valid_download_url:
            raise RuntimeError(f'failed to resolve download URL for {source} result')

        resolved = selected.todict()
        if source == 'NeteaseMusicClient' and not self._clean_nullish(resolved.get('lyric', ''), default=''):
            # Third-party URL resolvers often omit lyrics. Fetch only the
            # selected song's lyric after URL resolution, never during search.
            try:
                lyric_response = source_client.post(
                    'https://interface3.music.163.com/api/song/lyric',
                    data={
                        'id': str(raw_search.get('id')),
                        'cp': 'false', 'tv': '0', 'lv': '0', 'rv': '0',
                        'kv': '0', 'yv': '0', 'ytv': '0', 'yrv': '0',
                    },
                    timeout=10,
                    **request_overrides,
                )
                lyric_response.raise_for_status()
                lyric_payload = resp2json(lyric_response) or {}
                lyric_text = ((lyric_payload.get('lrc') or {}).get('lyric') or '') if isinstance(lyric_payload, dict) else ''
                if lyric_text:
                    resolved['lyric'] = lyric_text
                    resolved.setdefault('raw_data', {}).setdefault('lyric', {})
                    resolved['raw_data']['lyric'] = lyric_payload
            except Exception:
                pass

        if str(resolved.get('ext') or '').lower() != 'mp3' and source == 'NeteaseMusicClient':
            # Some third-party Netease resolvers ignore ``standard`` and
            # return Hi-Res/FLAC. Request the standard MP3 URL explicitly.
            try:
                response = requests.get(
                    'https://musicapi.haitangw.net/music/wy.php',
                    params={'id': str(raw_search.get('id')), 'level': 'standard', 'type': 'json'},
                    headers={'User-Agent': 'Mozilla/5.0'},
                    timeout=10,
                )
                response.raise_for_status()
                payload = resp2json(response) or {}
                data = payload.get('data') if isinstance(payload, dict) else None
                mp3_url = data.get('url') if isinstance(data, dict) else ''
                if isinstance(mp3_url, str) and mp3_url.startswith('http') and mp3_url.lower().split('?', 1)[0].endswith('.mp3'):
                    resolved['download_url'] = mp3_url
                    resolved['ext'] = 'mp3'
                    if isinstance(data, dict):
                        resolved['song_name'] = legalizestring(data.get('name') or resolved.get('song_name'))
                        resolved['singers'] = legalizestring(str(data.get('artist') or resolved.get('singers') or '').replace('/', ', '))
                        resolved['album'] = legalizestring(data.get('album') or resolved.get('album'))
                        resolved['cover_url'] = data.get('pic') or resolved.get('cover_url', '')
                        if data.get('size'):
                            resolved['file_size'] = str(data.get('size'))
                            size_match = re.match(r'\s*([0-9]+(?:\.[0-9]+)?)\s*(KB|MB|GB)\s*$', str(data.get('size')), re.IGNORECASE)
                            if size_match:
                                multiplier = {'KB': 1024, 'MB': 1024 ** 2, 'GB': 1024 ** 3}[size_match.group(2).upper()]
                                resolved['file_size_bytes'] = int(float(size_match.group(1)) * multiplier)
                        resolved.setdefault('raw_data', {}).setdefault('download', {}).update(data)
            except Exception:
                pass

        if str(resolved.get('ext') or '').lower() != 'mp3':
            raise RuntimeError(f'no MP3 download URL available for {source} result')
        return resolved

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
        if not raw_search:
            raw_data = song_info.get('raw_data') or {}
            if isinstance(raw_data, dict):
                raw_lyric = raw_data.get('lyric')
                if isinstance(raw_lyric, str) and raw_lyric.strip():
                    return raw_lyric
                raw_search = raw_data.get('search') or {}
                if not isinstance(raw_search, dict):
                    raw_search = {}
        raw_lrc = self._clean_nullish(raw_search.get('lrc', ''), default='')
        if not raw_lrc:
            return ''
        if raw_lrc.startswith('//'):
            return 'https:' + raw_lrc
        if raw_lrc.startswith('http://') or raw_lrc.startswith('https://'):
            return raw_lrc
        return ''

    def _resolve_cover_url(self, song_info: Dict) -> str:
        source = song_info.get('source', '')
        raw_search = song_info.get('raw_search') or {}
        if not isinstance(raw_search, dict):
            raw_search = {}
        if not raw_search:
            raw_data = song_info.get('raw_data') or {}
            if isinstance(raw_data, dict):
                raw_search = raw_data.get('search') or {}
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
                raw_search.get('hts_MVPIC'),
                raw_search.get('pic'),
            ]
            for item in kuwo_candidates:
                if not item:
                    continue
                item = str(item)
                if item.startswith('http') or item.startswith('//'):
                    candidates.append(item)
                else:
                    # Search returns paths such as ``120/s4s42/...jpg``.
                    # The old img4 HTTPS host now returns 404/514; kwcdn's
                    # HTTP album-cover endpoint is the working one.
                    cover_path = item.lstrip('/')
                    if cover_path.startswith('120/'):
                        cover_path = cover_path[4:]
                    candidates.extend([
                        f'http://img1.kwcdn.kuwo.cn/star/albumcover/240/{cover_path}',
                        f'http://img1.kwcdn.kuwo.cn/star/albumcover/500/{cover_path}',
                    ])

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
        cancel_check=None,
    ) -> str:
        try:
            with session.get(url, headers=headers or {}, stream=True, verify=False, timeout=(10, 3)) as resp:
                resp.raise_for_status()
                with open(save_path, 'wb') as fp:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if cancel_check and cancel_check():
                            raise RuntimeError('download cancelled')
                        if chunk:
                            fp.write(chunk)
        except Exception:
            try:
                if os.path.exists(save_path):
                    os.remove(save_path)
            except OSError:
                pass
            raise
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
            'KuwoMusicClient': 0,
            'NeteaseMusicClient': 1,
            'QQMusicClient': 2,
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

    def _create_download_session(self, source: str) -> requests.Session:
        session = requests.Session()
        client_obj = getattr(self.music_client, 'music_clients', {}).get(source) if self.music_client else None
        built_in_cookies = getattr(client_obj, 'default_download_cookies', {}) if client_obj else {}
        if isinstance(built_in_cookies, dict):
            session.cookies.update(built_in_cookies)
        return session

    def download_music(
        self,
        song_id: int,
        save_dir: str,
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

            session = self._create_download_session(source)

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
        download_cover: bool = True,
        download_lyric: bool = True,
        cancel_check=None,
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

            session = self._create_download_session(source)

            os.makedirs(save_dir, exist_ok=True)
            song_name = song_record.get('song_name') or 'song'
            ext = song_record.get('ext') or 'mp3'
            file_path = sanitize_filepath(os.path.join(save_dir, f'{song_name}.{ext}'))
            warnings: List[str] = []

            self._download_binary_file(session, download_url, file_path, headers=headers, cancel_check=cancel_check)
            file_path, ext, fix_warn = self._fix_file_extension(file_path, ext, song_name, save_dir)
            if fix_warn:
                warnings.append(fix_warn)

            cover_path = None
            cover_url = self._resolve_cover_url(song_record)
            if download_cover and cover_url.startswith('http'):
                cover_path = sanitize_filepath(os.path.join(save_dir, f'{song_name}.jpg'))
                try:
                    self._download_binary_file(session, cover_url, cover_path, headers=headers, cancel_check=cancel_check)
                except Exception:
                    if cancel_check and cancel_check():
                        raise
                    cover_path = None
                    warnings.append('cover download failed')

            lyric_path = None
            lyric_value = self._resolve_lyric_value(song_record)
            if download_lyric and lyric_value:
                lyric_path = sanitize_filepath(os.path.join(save_dir, f'{song_name}.lrc'))
                try:
                    lyric_text = ''
                    if isinstance(lyric_value, str) and lyric_value.startswith('http'):
                        if cancel_check and cancel_check():
                            raise RuntimeError('download cancelled')
                        with session.get(lyric_value, headers=headers, timeout=30, verify=False) as resp:
                            resp.raise_for_status()
                            lyric_text = resp.text
                    else:
                        lyric_text = str(lyric_value)
                    with open(lyric_path, 'w', encoding='utf-8') as fp:
                        fp.write(lyric_text)
                except Exception:
                    if cancel_check and cancel_check():
                        raise
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

    test_keyword = '春雪'
    print(f'[TEST] search keyword: {test_keyword}')
    search_result = service.search_music(
        test_keyword,
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
        print(f'[TEST] try downloading song_id={test_song_id}')
        try:
            download_result = service.download_music(
                song_id=test_song_id,
                save_dir=test_save_dir,
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
