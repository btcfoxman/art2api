"""Seedance web protocol mapping derived from the operator's 2026-09-13 CDP capture."""
from __future__ import annotations

import json
from pathlib import Path

from app.catalog import PUBLIC_MODELS

SNAPSHOT = json.loads(Path(__file__).with_name('web_models.json').read_text(encoding='utf-8'))
GROUPS = {
    'doubao-seedance-2-0-fast-260128': 377,
    'doubao-seedance-2-0-260128': 358,
    'doubao-seedance-2-0-260128-4k': 358,
    'doubao-seedance-2-0-mini-260615': 416,
    'doubao-seedance-2-0-fast-260128-480p': 377,
    'doubao-seedance-2-0-260128-480p': 358,
    'doubao-seedance-2-0-260128-1080p': 358,
    'sd-2-5': 515, 'sd-2-5-480p': 515, 'sd-2-5-1080p': 515,
}


def profiles():
    result = {}
    for model, group in GROUPS.items():
        fields = SNAPSHOT['groups'][str(group)]['settings']
        fixed, _ = PUBLIC_MODELS[model]
        result[model] = {
            'backend': 'web', 'group_id': group,
            'validation': 'generation_observed' if model in {'sd-2-5-480p', 'doubao-seedance-2-0-fast-260128-480p'} else 'quote_verified',
            'constraints': {
                'durations': [int(v) for v in fields['duration']['values']],
                'resolutions': [fixed] if fixed else fields['resolution']['values'],
                'aspect_ratios': fields['aspect_ratio']['values'],
                'max_images': 50 if group == 515 else 9,
                'max_videos': 10 if group == 515 else 3, 'max_audios': 10 if group == 515 else 3,
            },
        }
    return result


def validate_request(request, profile):
    for field, key in [('duration', 'durations'), ('resolution', 'resolutions'), ('aspect_ratio', 'aspect_ratios')]:
        if request[field] not in profile['constraints'][key]:
            raise ValueError(f'Artlist 网页模型不支持 {field}={request[field]}')
    for field, key in [('image_urls', 'max_images'), ('video_urls', 'max_videos'), ('audio_urls', 'max_audios')]:
        if len(request[field]) > profile['constraints'][key]:
            raise ValueError(f'Artlist 网页模型 {field} 数量超限')
    if profile['group_id'] != 515 and sum(len(request[k]) for k in ('image_urls', 'video_urls', 'audio_urls')) > 12:
        raise ValueError('Seedance 2.0 素材总数不能超过 12')
    if request.get('first_frame') and any(request[k] for k in ('image_urls', 'video_urls', 'audio_urls')):
        raise ValueError('首尾帧模式不能与多参考素材混用')
    if 'seed' in request or 'fps' in request:
        raise ValueError('网页抓包未提供 seed/fps 设置，不能静默忽略')
    if 'generate_audio' in request and not isinstance(request['generate_audio'], bool):
        raise ValueError('generate_audio 必须为布尔值')


def quote_input(request, assets):
    group = GROUPS[request['model']]
    defaults = SNAPSHOT['groups'][str(group)]['defaults']
    settings = {k: request[k] for k in ('prompt', 'duration', 'resolution', 'aspect_ratio')}
    settings['generate_audio'] = request.get('generate_audio', defaults.get('generate_audio') == 'true')
    if 'generation_mode' in defaults:
        modes = SNAPSHOT['groups'][str(group)]['settings']['generation_mode']['values']
        settings['generation_mode'] = next((v for v in modes if str(v).lower() == 'endframe'), 'endFrame') if request.get('first_frame') else 'references'
    inputs = {'prompt': request['prompt']}
    artifacts, metadata, tags = [], {}, []
    for field, values in assets.items():
        if not values:
            continue
        wire = {'first_frame': 'image_url', 'last_frame': 'end_frame'}.get(field, field)
        media = [{'fileUrl': a['file_url'], **({'url': a['file_url']} if field in {'video_urls', 'audio_urls'} else {})} for a in values]
        inputs[wire] = values[0]['file_url'] if field in {'first_frame', 'last_frame'} else media
        if field in {'image_urls', 'video_urls', 'audio_urls'}:
            prefix = {'image_urls': '@img', 'video_urls': '@vid', 'audio_urls': '@aud'}[field]
            tags.extend({'tagId': f'{prefix}{i}', 'type': prefix, 'orderForType': i} for i in range(1, len(values)+1))
        if field in {'video_urls', 'audio_urls'}:
            metadata[field] = [{'duration': a['metadata']['durationMs']/1000} for a in values]
        for asset in values:
            artifacts.append({'fileKey': asset['file_key'], 'metadata': {
                **{k:v for k,v in asset['metadata'].items() if k != 'fps'}, 'fileUrl': asset['file_url'], 'inputSettingKey': wire,
                'fileType': 'deviceUpload',
            }})
    if tags:
        inputs['tagReferences'] = tags
        settings['tagReferences'] = tags
    if metadata:
        settings['user_inputs_metadata'] = metadata
    # Quotes require the same media, settings and durations as the eventual submit.
    return {'modelGroupId': group, 'input': {**settings, **inputs}}, inputs, settings, artifacts


def generation_payload(session_id, quote, inputs, settings, artifacts, verification_token):
    if not verification_token:
        raise ValueError('网页提交需要当次正常验证令牌')
    return {'chatSessionId': session_id, 'inputs': inputs, 'modelGroupId': quote['modelId'],
            'feature': quote['modelFeature'], 'price': quote['cost'], 'settings': settings, 'artifacts': artifacts,
            'costQuoteDigitalSignature': quote['digitalSignature'], 'timestamp': quote['timestamp'],
            'generationMethod': 'credits', 'isCopyCmsFileEnabled': False, 'turnstileToken': verification_token}


def generation_result(data):
    state = str(data.get('status', '')).lower()
    if state in {'failed', 'error', 'cancelled', 'canceled', 'rejected'}:
        return {'status': 'failed', 'error_code': 'generation_failed'}
    if state in {'completed', 'succeeded', 'success'}:
        outputs = data.get('outputs') or []
        output = next((o for o in outputs if o.get('fileUrl') and o.get('generationType') == 'generatedVideo'), None)
        if not output:
            output = next((o for o in outputs if o.get('fileUrl')), None)
        if output:
            return {'status': 'succeeded', 'video_url': output['fileUrl'], 'output_id': output.get('id', ''),
                    'metadata': output.get('metadata'), 'generation_id': data.get('id', '')}
        # A completed generation may publish its output slightly later.
        return {'status': 'running', 'upstream_status': state}
    if state in {'pending', 'queued', 'processing', 'inprogress', 'in_progress', 'running', 'created', 'submitted', 'generating'}:
        return {'status': 'running', 'upstream_status': state}
    from app.errors import GatewayError
    raise GatewayError('Artlist 返回未知任务状态，继续按原 ID 查询', 'web_status_pending', retryable=True)
