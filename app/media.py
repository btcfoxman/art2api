"""Local video preparation for Artlist modes that inherit reference geometry."""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path


async def run_media_command(*args, timeout=180):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode:
            raise ValueError('参考视频转换失败，请检查素材是否完整')
        return stdout
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def probe(path):
    try:
        data = await run_media_command('ffprobe', '-v', 'error', '-show_streams', '-show_format',
                                       '-of', 'json', str(path), timeout=30)
        return json.loads(data)
    except asyncio.TimeoutError:
        raise ValueError('参考素材信息解析超时') from None


def reference_spec(metadata, request):
    width, height = metadata.get('width', 0), metadata.get('height', 0)
    duration = metadata.get('durationMs', 0) / 1000
    if min(width, height, duration) <= 0:
        raise ValueError('参考视频缺少有效尺寸或时长')
    a, b = (int(value) for value in request['aspect_ratio'].split(':'))
    ratio = a / b
    # Keep the source dimensions when already compliant. Container duration can
    # exceed the nominal frame duration slightly, e.g. a 4s MP4 reports 4.096s.
    ratio_matches = abs(width / height / ratio - 1) <= .01
    duration_matches = abs(duration - request['duration']) <= .25
    resolution_matches = 720 <= min(width, height) <= 1080
    return ratio, ratio_matches, duration_matches, resolution_matches


async def adapt_reference_video(path: Path, metadata, request, policy):
    ratio, ratio_matches, duration_matches, resolution_matches = reference_spec(metadata, request)
    if ratio_matches and duration_matches and resolution_matches and 24 <= metadata.get('fps', 0) <= 60:
        return path, metadata, None
    before = {key: metadata.get(key) for key in ('width', 'height', 'durationMs', 'fps')}
    if policy == 'strict':
        raise ValueError(f"Seedance 2.5 参考视频模式跟随素材规格：当前为 {metadata['width']}×{metadata['height']}、"
                         f"{metadata['durationMs']/1000:g} 秒，请求为 {request['aspect_ratio']}、{request['duration']} 秒；"
                         '请调整请求，或在运行设置中启用参考视频自动适配')
    short = 720
    width = round(short * max(ratio, 1) / 2) * 2
    height = round(short * max(1 / ratio, 1) / 2) * 2
    duration = request['duration']
    source_duration = metadata['durationMs'] / 1000
    speed = source_duration / duration
    tempo = speed
    tempo_filters = []
    while tempo < .5:
        tempo_filters.append('atempo=0.5')
        tempo /= .5
    while tempo > 2:
        tempo_filters.append('atempo=2')
        tempo /= 2
    tempo_filters.append(f'atempo={tempo:.10f}')
    audio_filter = ','.join(['asetpts=PTS-STARTPTS', *tempo_filters, 'apad'])
    output = path.with_name(path.stem + '-adapted.mp4')
    video_filter = (f'setpts={1/speed:.10f}*(PTS-STARTPTS),'
                    f'scale={width}:{height}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2,'
                    f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,'
                    'tpad=stop_mode=clone:stop_duration=1')
    try:
        await run_media_command('ffmpeg', '-nostdin', '-y', '-v', 'error', '-i', str(path),
                                '-map', '0:v:0', '-map', '0:a:0?', '-vf', video_filter, '-af', audio_filter,
                                '-t', str(duration), '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
                                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', '-threads', '2',
                                '-movflags', '+faststart', str(output))
    except asyncio.TimeoutError:
        raise ValueError('参考视频转换超时') from None
    media = await probe(output)
    visual = next((s for s in media.get('streams', []) if s.get('codec_type') == 'video'), {})
    actual_duration = round(float(media.get('format', {}).get('duration', 0))*1000)
    if (visual.get('width') != width or visual.get('height') != height
            or abs(actual_duration/1000-duration) > .25):
        raise ValueError('参考视频转换后的规格不符合请求')
    result = {**metadata, 'fileName': output.name, 'mimeType': 'video/mp4', 'byteSize': output.stat().st_size,
              'width': width, 'height': height, 'durationMs': actual_duration, 'fps': 30.0}
    actions = []
    if not ratio_matches:
        actions.append('缩放并加黑边匹配比例，保留完整画面')
    if not duration_matches:
        actions.append(f'整段视频与音轨同步变速至目标时长（{speed:.4f} 倍速）')
    actions.append('转为 MP4 / H.264 / 30fps，参考视频短边 720px')
    record = {'policy': 'adjust', 'speed': round(speed, 6), 'before': before,
              'after': {key: result[key] for key in ('width', 'height', 'durationMs', 'fps')}, 'actions': actions}
    return output, result, record
