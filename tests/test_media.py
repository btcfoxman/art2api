import hashlib
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.catalog import normalize_request
from app.media import adapt_reference_video, probe, run_media_command
from app.web_catalog import quote_input


@pytest.mark.asyncio
async def test_matching_reference_passes_unchanged_and_strict_conflict_is_clear():
    path = Path('original.mp4')
    request = {'duration': 4, 'aspect_ratio': '9:16'}
    metadata = {'width': 720, 'height': 1280, 'durationMs': 4096, 'fps': 30}
    with patch('app.media.run_media_command', AsyncMock()) as command:
        assert await adapt_reference_video(path, metadata, request, 'strict') == (path, metadata, None)
        with pytest.raises(ValueError, match='跟随素材规格'):
            await adapt_reference_video(path, metadata, {'duration': 8, 'aspect_ratio': '16:9'}, 'strict')
        command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='ffmpeg and ffprobe required')
@pytest.mark.parametrize('source_duration,with_audio,action', [(4.5, True, '整段视频'), (1, False, '整段视频')])
async def test_video_adaptation_matches_output_spec_and_preserves_source(tmp_path, source_duration, with_audio, action):
    original = tmp_path/'source.mp4'
    args = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', f'color=c=blue:s=120x160:r=30:d={source_duration}']
    if with_audio:
        args += ['-f', 'lavfi', '-i', f'sine=frequency=440:duration={source_duration}', '-c:a', 'aac']
    await run_media_command(*args, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-threads', '1', str(original))
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    metadata = {'width':120, 'height':160, 'durationMs':round(source_duration*1000), 'fps':30,
                'fileName':'source.mp4', 'mimeType':'video/mp4', 'byteSize':original.stat().st_size}
    request = {'duration':4, 'aspect_ratio':'16:9'}
    output, after, record = await adapt_reference_video(original, metadata, request, 'adjust')
    assert output != original and hashlib.sha256(original.read_bytes()).hexdigest() == digest
    assert (after['width'], after['height']) == (1280, 720)
    assert abs(after['durationMs']-4000) <= 250
    assert any(action in value for value in record['actions'])
    assert record['speed'] == source_duration / 4
    streams = (await probe(output))['streams']
    assert any(s['codec_type']=='audio' for s in streams) == with_audio
    assert 'fileUrl' not in str(record) and record['before']['width']==120


def test_sd25_reference_video_uses_auto_in_quote_and_submission_settings():
    request = normalize_request({'model':'sd-2-5-480p','prompt':'move naturally','duration':8,
                                  'aspect_ratio':'16:9','video_urls':['https://example.com/video.mp4']})
    assets = {'video_urls':[{'file_key':'video', 'file_url':'https://storage.example/video',
                             'metadata':{'width':1280,'height':720,'durationMs':8000}}]}
    quote, inputs, settings, _ = quote_input(request, assets)
    assert quote['input']['aspect_ratio'] == settings['aspect_ratio'] == 'auto'
    assert settings['user_inputs_metadata']['video_urls'] == [{'duration':8}]
    assert request['aspect_ratio']=='16:9' and request['duration']==8
    request['model']='doubao-seedance-2-0-mini-260615'
    assert quote_input(request, assets)[2]['aspect_ratio']=='16:9'
