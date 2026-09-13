"""Seedance web protocol mapping derived from the operator's 2026-09-13 CDP capture."""
from __future__ import annotations

import json
import re
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
            'validation': 'generation_observed' if model in {'sd-2-5-480p', 'doubao-seedance-2-0-fast-260128-480p', 'doubao-seedance-2-0-mini-260615'} else 'quote_verified',
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
    reference_prompt(request)


def reference_prompt(request):
    """Translate client reference labels, keeping their original asset indices.

    Artlist rejects tagReferences whose tagId isn't present in the prompt.
    Unmentioned assets remain inputs but must not invent reference tags.
    """
    prompt = request['prompt']
    if request.get('first_frame'):
        return prompt, []
    referenced = set()
    for field, prefix, aliases in [
        ('image_urls', '@img', r'img|image|参考(?:图片|图像|图)?|图片|图像|图'),
        ('video_urls', '@vid', r'vid|video|(?:参考)?视频'),
        ('audio_urls', '@aud', r'aud|audio|(?:参考)?音频|声音|语音'),
    ]:
        def replace(match):
            index = int(match.group(1))
            if not 1 <= index <= len(request.get(field) or []):
                raise ValueError(f'提示词引用的 {prefix}{index} 没有对应素材')
            tag = f'{prefix}{index}'
            referenced.add((field, prefix, index))
            return tag
        prompt = re.sub(r'@(?:'+aliases+r')(\d+)(?![0-9A-Za-z_])', replace, prompt, flags=re.IGNORECASE)
    order = {'image_urls':0, 'video_urls':1, 'audio_urls':2}
    tags = [{'tagId':f'{prefix}{index}', 'type':prefix, 'orderForType':index}
            for field,prefix,index in sorted(referenced,key=lambda value:(order[value[0]],value[2]))]
    header = audio_reference_header(request, tags)
    if header and not prompt.startswith(header):
        # Seedance 2.5 can fail mapping audio references near the end of a
        # long prompt. Keep a leading index of ONLY already-mentioned audio
        # tags, retaining the complete prompt and the original asset indices.
        prompt = header + prompt
    return prompt, tags


def audio_reference_header(request, tags):
    audio = [tag['tagId'] for tag in tags if tag['type'] == '@aud']
    return ' '.join(audio) + '\n' if GROUPS[request['model']] == 515 and audio else ''


def quote_input(request, assets):
    group = GROUPS[request['model']]
    defaults = SNAPSHOT['groups'][str(group)]['defaults']
    settings = {k: request[k] for k in ('prompt', 'duration', 'resolution', 'aspect_ratio')}
    prompt, tags = reference_prompt(request)
    settings['prompt'] = prompt
    settings['generate_audio'] = request.get('generate_audio', defaults.get('generate_audio') == 'true')
    # Seedance 2.5's reference-video submodel requires auto and inherits the
    # uploaded video's shape/duration. prepare() first adapts or validates it.
    if group == 515 and assets.get('video_urls'):
        settings['aspect_ratio'] = 'auto'
    if 'generation_mode' in defaults:
        modes = SNAPSHOT['groups'][str(group)]['settings']['generation_mode']['values']
        settings['generation_mode'] = next((v for v in modes if str(v).lower() == 'endframe'), 'endFrame') if request.get('first_frame') else 'references'
    inputs = {'prompt': prompt}
    artifacts, metadata = [], {}
    for field, values in assets.items():
        if not values:
            continue
        wire = {'first_frame': 'image_url', 'last_frame': 'end_frame'}.get(field, field)
        media = [{'fileUrl': a['file_url'], **({'url': a['file_url']} if field in {'video_urls', 'audio_urls'} else {})} for a in values]
        inputs[wire] = values[0]['file_url'] if field in {'first_frame', 'last_frame'} else media
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


def generation_payload(session_id, quote, inputs, settings, artifacts, verification_token=''):
    return {'chatSessionId': session_id, 'inputs': inputs, 'modelGroupId': quote['modelId'],
            'feature': quote['modelFeature'], 'price': quote['cost'], 'settings': settings, 'artifacts': artifacts,
            'costQuoteDigitalSignature': quote['digitalSignature'], 'timestamp': quote['timestamp'],
            'generationMethod': 'credits', 'isCopyCmsFileEnabled': False,
            **({'turnstileToken': verification_token} if verification_token else {})}


def generation_result(data):
    state = str(data.get('status', '')).lower()
    if state in {'failed', 'error', 'cancelled', 'canceled', 'rejected'}:
        code = str(data.get('errorCode') or '')
        code = code if re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}',code) else ''
        # Reasons can contain signed media URLs. Expose the structured code and
        # a safe explanation, never the arbitrary upstream reason string.
        message = 'Artlist 无法读取参考素材' if code=='INPUT_URL_UNREACHABLE' else 'Artlist 明确报告生成失败'
        if code == 'PROVIDER_CONTENT_SAFETY_VIOLATION':
            message = 'Artlist 内容审核未通过，请检查提示词或参考素材'
            if 'OutputAudioSensitiveContentDetected' in str(data.get('reason', '')):
                message = 'Artlist 输出音频审核未通过'
                if 'copyright' in str(data.get('reason', '')).lower():
                    message = 'Artlist 拒绝输出音频：可能涉及版权限制，请调整音频相关请求或参考素材'
            elif 'OutputVideoSensitiveContentDetected' in str(data.get('reason', '')):
                message = 'Artlist 输出视频审核未通过'
                if 'copyright' in str(data.get('reason', '')).lower():
                    message = 'Artlist 拒绝输出视频：可能涉及版权限制，请调整相关请求或参考素材'
        if code == 'FILE_OPTIMIZATION_FAILED':
            message = 'Artlist 参考素材预处理失败'
            reason = str(data.get('reason', '')).lower()
            if 'input audio' in reason and 'sensitive' in reason:
                message = 'Artlist 参考音频审核未通过，请检查音频素材'
        if code=='MAP_INTERNAL_REQUEST_TO_PROVIDER_REQUEST_FAILED' and 'Reference tag not found in prompt' in str(data.get('reason','')):
            message = 'Artlist 提示词引用标签与素材不一致'
        if code:
            message += f'（{code}）'
        return {'status':'failed','error_code':'generation_failed','error_message':message,
                'upstream_error_code':code,'upstream_status':state}
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
