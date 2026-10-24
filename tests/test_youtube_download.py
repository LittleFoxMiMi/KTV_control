"""Exercise download retries without opening the live database or Huey queue."""
import ast
from contextlib import nullcontext
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import traceback
import unittest
from unittest.mock import Mock


def load_download_code():
    source = Path(__file__).resolve().parents[1] / 'src' / 'tasks.py'
    names = {
        '_display_resolution', '_format_size_bytes', '_select_bilibili_video_formats',
        '_select_youtube_video_formats', '_select_best_audio_format', '_media_paths',
        '_validate_media_file', '_remux_video_for_streaming', '_validate_audio_file',
        '_remove_files_with_prefix', 'download_song_task',
    }
    nodes = [node for node in ast.parse(source.read_text(encoding='utf-8')).body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    namespace = dict(os=os, re=re, json=json, shutil=shutil, subprocess=subprocess,
                     traceback=traceback, VIDEO_EXTENSIONS=('.mp4', '.webm', '.mkv'),
                     MAX_VIDEO_SIZE_BYTES=200_000_000)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


class YouTubeDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.code = load_download_code()
        self.code.update(
            config={'paths': {'songs_dir': self.temp.name, 'ffmpeg_path': 'ffmpeg', 'yt_dlp_path': 'yt-dlp'}},
            db=Mock(), _task_lock=lambda name: nullcontext(),
            get_song_task_priority=Mock(return_value=1), separate_song_task=Mock(),
        )
        self.code['_get_song'] = Mock(return_value={
            'video_id': 'sample', 'title': 'sample', 'status': 'pending',
            'url': 'https://www.youtube.com/watch?v=sample',
        })
        formats = [
            {'format_id': 'audio', 'vcodec': 'none', 'acodec': 'opus'},
            {'format_id': 'fallback', 'vcodec': 'vp9', 'acodec': 'none', 'width': 1280, 'height': 720},
            {'format_id': 'preferred', 'vcodec': 'vp9', 'acodec': 'none', 'width': 1920, 'height': 1080},
        ]
        self.code['subprocess'] = Mock()
        self.code['subprocess'].run.return_value = Mock(returncode=0, stdout=json.dumps({'formats': formats}))
        self.downloads = []

        def download(command, *args, **kwargs):
            video_format, audio_format = command[command.index('-f') + 1].split(',')
            template = command[command.index('-o') + 1]
            staging = Path(template).parent
            # No failed attempt's source, audio, or partially remuxed file survives.
            self.assertEqual(list(staging.iterdir()), [])
            (staging / f'sample.{video_format}.webm').write_bytes(b'downloaded video')
            (staging / f'sample.{audio_format}.webm').write_bytes(b'downloaded audio')
            self.downloads.append(video_format)
            return 0

        self.code['run_command_with_progress'] = Mock(side_effect=download)
        self.code['_validate_media_file'] = Mock(return_value=(True, ''))
        self.code['_validate_audio_file'] = Mock(return_value=(True, ''))

        def remux(source, output):
            Path(output).write_bytes(b'remuxed video')
            return True, ''

        self.code['_remux_video_for_streaming'] = Mock(side_effect=remux)

    def assert_saved_remux(self):
        args = self.code['db'].update_paths.call_args.kwargs
        saved = Path(self.temp.name) / args['video_path']
        self.assertEqual(saved.suffix, '.mp4')
        self.assertEqual(saved.read_bytes(), b'remuxed video')
        self.code['separate_song_task'].assert_called_once_with(7, priority=1)
        self.assertFalse((saved.parent / '.download').exists())

    def test_only_validated_remux_is_published(self):
        self.code['download_song_task'](7)
        self.assertEqual(self.downloads, ['preferred'])
        self.assert_saved_remux()
        post_validation = self.code['_validate_media_file'].call_args
        self.assertTrue(post_validation.args[0].endswith('.faststart.mp4'))
        self.assertEqual(post_validation.kwargs, {'full_decode': True})

    def test_remux_failure_cleans_partial_output_and_retries(self):
        successful_remux = self.code['_remux_video_for_streaming'].side_effect

        def remux(source, output):
            if 'preferred' in source:
                Path(output).write_bytes(b'partial output')
                return False, 'remux failed'
            return successful_remux(source, output)

        self.code['_remux_video_for_streaming'].side_effect = remux
        self.code['download_song_task'](7)
        self.assertEqual(self.downloads, ['preferred', 'fallback'])
        self.assert_saved_remux()

    def test_post_remux_validation_failure_retries(self):
        self.code['_validate_media_file'].side_effect = lambda path, **kwargs: (
            (False, 'decode error') if 'preferred.faststart' in path else (True, '')
        )
        self.code['download_song_task'](7)
        self.assertEqual(self.downloads, ['preferred', 'fallback'])
        self.assert_saved_remux()

    def test_all_post_remux_validation_failures_publish_nothing(self):
        self.code['_validate_media_file'].side_effect = lambda path, **kwargs: (
            (False, 'decode error') if kwargs.get('full_decode') else (True, '')
        )
        self.code['download_song_task'](7)
        self.assertEqual(self.downloads, ['preferred', 'fallback'])
        self.code['db'].update_paths.assert_not_called()
        self.code['separate_song_task'].assert_not_called()
        last = self.code['db'].update_status.call_args
        self.assertEqual(last.args[1], 'error')
        self.assertIn('remux validation failed', last.kwargs['error_msg'])
        self.assertFalse((Path(self.temp.name) / 'youtube/sample/.download').exists())

    def test_all_remux_failures_publish_nothing(self):
        self.code['_remux_video_for_streaming'].side_effect = None
        self.code['_remux_video_for_streaming'].return_value = (False, 'remux failed')
        self.code['download_song_task'](7)
        self.assertEqual(self.downloads, ['preferred', 'fallback'])
        self.code['db'].update_paths.assert_not_called()
        self.code['separate_song_task'].assert_not_called()
        last = self.code['db'].update_status.call_args
        self.assertEqual(last.args[1], 'error')
        self.assertIn('remux failed', last.kwargs['error_msg'])
        self.assertFalse((Path(self.temp.name) / 'youtube/sample/.download').exists())

    def test_bilibili_keeps_existing_download_behavior(self):
        self.code['_get_song'].return_value['url'] = 'https://www.bilibili.com/video/sample'
        self.code['download_song_task'](7)
        self.code['_remux_video_for_streaming'].assert_not_called()
        self.assertEqual(self.downloads, ['preferred'])
        self.assertFalse(any(call.kwargs for call in self.code['_validate_media_file'].call_args_list))
        args = self.code['db'].update_paths.call_args.kwargs
        self.assertEqual((Path(self.temp.name) / args['video_path']).read_bytes(), b'downloaded video')


if __name__ == '__main__':
    unittest.main()
