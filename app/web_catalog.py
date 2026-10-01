"""Seedance web protocol mapping derived from the operator's 2026-09-13 CDP capture."""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.catalog import PUBLIC_MODELS
from app.public_errors import public_message

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
    for field, prefix, aliases, plain_aliases in [
        ('image_urls', '@img', r'img|image|参考(?:图片|图像|图)?|图片|图像|图', r'参考(?:图片|图像|图)|图片|图像|图'),
        ('video_urls', '@vid', r'vid|video|(?:参考)?视频', r'(?:参考)?视频'),
        ('audio_urls', '@aud', r'aud|audio|(?:参考)?音频|声音|语音', r'(?:参考)?音频'),
    ]:
        def replace(match):
            index = int(match.group(1))
            if not 1 <= index <= len(request.get(field) or []):
                raise ValueError(f'提示词引用的 {prefix}{index} 没有对应素材')
            tag = f'{prefix}{index}'
            referenced.add((field, prefix, index))
            return tag
        prompt = re.sub(r'@(?:'+aliases+r')(\d+)(?![0-9A-Za-z_])', replace, prompt, flags=re.IGNORECASE)
        if request.get(field):
            # Prompts commonly use 图1 / 音频1 without an @ prefix. Bind those
            # numbered assets too, while leaving generic "参考图片" text alone.
            prompt = re.sub(r'(?<![@0-9A-Za-z])(?:'+plain_aliases+r')(\d+)(?![0-9A-Za-z_])',
                            replace, prompt)
    order = {'image_urls':0, 'video_urls':1, 'audio_urls':2}
    tags = [{'tagId':f'{prefix}{index}', 'type':prefix, 'orderForType':index}
            for field,prefix,index in sorted(referenced,key=lambda value:(order[value[0]],-len(str(value[2])),value[2]))]
    header = reference_header(request, tags)
    if header and not prompt.startswith(header):
        # Give upstream mapping an early, whitespace-delimited occurrence of
        # every used tag. The header uses natural indices; the mapping list
        # handles longer tags first. Asset bindings remain unchanged.
        prompt = header + prompt
    return prompt, tags


def reference_header(request, tags):
    # Preserve the proven leading-audio workaround when other media are present.
    # The leading index uses natural order: an upstream substring lookup for
    # @img1 must encounter its standalone occurrence before any @img10 token.
    order = {'@aud': 0, '@img': 1, '@vid': 2}
    ordered = sorted(tags, key=lambda tag: (order[tag['type']], tag['orderForType']))
    return ' '.join(tag['tagId'] for tag in ordered) + '\n' if tags else ''


_DIALOGUE = re.compile(r'(?:台词|对白|配音指令)[^\r\n：:]{0,80}[：:]\s*(?:[^“「『"‘\'\r\n]{1,20}[：:]\s*)?[“「『"‘\']([^”」』"’\'\r\n]{1,200})')
_SPOKEN_QUOTE = re.compile(r'(?:说(?:完整原句|原句|台词|对白|出)?|喊(?:出)?|念(?:出)?|讲(?:出)?)[^。！？\r\n“「『"‘\']{0,24}[：:]\s*[“「『"‘\']([^”」』"’\'\r\n]{1,200})')


def speech_language_prompt(prompt):
    """Label quoted dialogue at the line itself when the language is implicit.

    The web model has no speech-language setting. In an otherwise identical
    generation, adding 日文 before each 台词 produced the requested Japanese.
    """
    labels = []
    for match in _DIALOGUE.finditer(prompt):
        start = match.start()
        line_prefix = prompt[prompt.rfind('\n', 0, start) + 1:start]
        if any(language in line_prefix for language in ('日文', '日语', '日本語', '英文', '英语', '中文', '汉语', '漢語')):
            continue
        dialogue = match.group(1)
        if any('\u3040' <= char <= '\u30ff' for char in dialogue):
            language = '日文'
        elif any('\u4e00' <= char <= '\u9fff' for char in dialogue):
            language = '中文'
        else:
            continue
        labels.append((start, language))
    # Narrative prompts often say “说完整原句：...” without the literal 台词
    # label. Put the language and articulation instruction beside that speech
    # cue, while leaving the requested quotation and timing untouched.
    labeled_quotes = {match.start(1) for match in _DIALOGUE.finditer(prompt)}
    for match in _SPOKEN_QUOTE.finditer(prompt):
        if match.start(1) in labeled_quotes:
            continue
        line_prefix = prompt[prompt.rfind('\n', 0, match.start()) + 1:match.start(1)]
        if any(language in line_prefix for language in ('日文', '日语', '日本語', '英文', '英语', '中文', '汉语', '普通话', '漢語')):
            continue
        dialogue = match.group(1)
        if any('\u3040' <= char <= '\u30ff' for char in dialogue):
            label = '（日语，逐字清晰）'
        elif any('\u4e00' <= char <= '\u9fff' for char in dialogue):
            label = '（中文普通话，逐字清晰，不翻译不改写）'
        else:
            continue
        colon = max(prompt.rfind('：', match.start(), match.start(1)),
                    prompt.rfind(':', match.start(), match.start(1)))
        labels.append((colon, label))
    for start, language in reversed(labels):
        prompt = prompt[:start] + language + prompt[start:]
    return prompt


def quote_input(request, assets):
    group = GROUPS[request['model']]
    defaults = SNAPSHOT['groups'][str(group)]['defaults']
    settings = {k: request[k] for k in ('prompt', 'duration', 'resolution', 'aspect_ratio')}
    prompt, tags = reference_prompt(request)
    settings['generate_audio'] = request.get('generate_audio', defaults.get('generate_audio') == 'true')
    if settings['generate_audio']:
        prompt = speech_language_prompt(prompt)
    settings['prompt'] = prompt
    # Seedance 2.5's reference-video and start/end-frame submodels require
    # auto. The selected submodel derives shape from the uploaded media.
    if group == 515 and (assets.get('video_urls') or assets.get('first_frame')):
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
        if code == 'INSUFFICIENT_CREDITS':
            message = 'Artlist 账号可用积分不足，请检查账号额度'
        if code == 'PROVIDER_CONTENT_SAFETY_VIOLATION':
            message = 'Artlist 内容审核未通过，请检查提示词或参考素材'
            reason = str(data.get('reason', ''))
            if 'InputVideoSensitiveContentDetected' in reason:
                message = 'Artlist 参考视频审核未通过，请更换参考视频'
            elif 'OutputAudioSensitiveContentDetected' in reason:
                message = 'Artlist 输出音频审核未通过'
                if 'copyright' in reason.lower():
                    message = 'Artlist 拒绝输出音频：可能涉及版权限制，请调整音频相关请求或参考素材'
            elif 'OutputVideoSensitiveContentDetected' in reason:
                message = 'Artlist 输出视频审核未通过'
                if 'copyright' in reason.lower():
                    message = 'Artlist 拒绝输出视频：可能涉及版权限制，请调整相关请求或参考素材'
        if code == 'FILE_OPTIMIZATION_FAILED':
            message = 'Artlist 参考素材预处理失败'
            reason = str(data.get('reason', '')).lower()
            for media, label in [('video', '视频'), ('audio', '音频'), ('image', '图片')]:
                if f'input {media}' not in reason:
                    continue
                if 'copyright' in reason:
                    message = f'Artlist 拒绝参考{label}：可能涉及版权限制，请检查{label}素材'
                elif 'sensitive' in reason:
                    message = f'Artlist 参考{label}审核未通过，请检查{label}素材'
                break
        if code=='MAP_INTERNAL_REQUEST_TO_PROVIDER_REQUEST_FAILED' and 'Reference tag not found in prompt' in str(data.get('reason','')):
            message = 'Artlist 提示词引用标签与素材不一致'
        if code:
            message += f'（{code}）'
        return {'status':'failed','error_code':'generation_failed','error_message':message,
                'public_error_message':public_message('generation_failed', message+' '+str(data.get('reason', '')), code),
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
