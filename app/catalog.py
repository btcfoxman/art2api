from __future__ import annotations

import json

from jsonschema import Draft202012Validator, ValidationError

from app.network import public_media_url


PUBLIC_MODELS = {
    'doubao-seedance-2-0-260128': (None, 15),
    'doubao-seedance-2-0-fast-260128': (None, 15),
    'doubao-seedance-2-0-mini-260615': (None, 15),
    'doubao-seedance-2-0-fast-260128-480p': ('480p', 15),
    'doubao-seedance-2-0-260128-480p': ('480p', 15),
    'doubao-seedance-2-0-260128-1080p': ('1080p', 15),
    'doubao-seedance-2-0-260128-4k': ('4k', 15),
    'sd-2-5': ('720p', 30), 'sd-2-5-480p': ('480p', 30), 'sd-2-5-1080p': ('1080p', 30),
}
ALIASES = {'doubao-seedance-2-5': 'sd-2-5', 'seedance-2.5': 'sd-2-5'}
INPUT_FIELDS = {'model', 'prompt', 'duration', 'resolution', 'aspect_ratio', 'image_urls', 'video_urls',
                'audio_urls', 'first_frame', 'last_frame', 'generate_audio', 'seed', 'fps'}


def local_schema(schema):
    # JSON Schema validation must never fetch external references outside the account proxy.
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in {'$ref', '$dynamicRef'} and isinstance(value, str) and not value.startswith('#'):
                raise ValueError('不支持需要联网解析的外部 JSON Schema 引用')
            local_schema(value)
    elif isinstance(schema, list):
        for item in schema:
            local_schema(item)
    return schema


def normalize_request(payload):
    model = str(payload.get('model') or payload.get('model_id') or '')
    model = ALIASES.get(model.removeprefix('ark_'), model.removeprefix('ark_'))
    if model not in PUBLIC_MODELS:
        raise ValueError('首版仅支持已登记的 Seedance 视频模型')
    fixed, max_duration = PUBLIC_MODELS[model]
    request = {'model': model, 'prompt': str(payload.get('prompt') or '').strip(),
               'duration': int(payload.get('duration', 5)), 'resolution': fixed or str(payload.get('resolution') or '720p').lower(),
               'aspect_ratio': str(payload.get('aspect_ratio') or payload.get('ratio') or '16:9')}
    if not request['prompt'] or len(request['prompt']) > 20000:
        raise ValueError('prompt 必填，且不能超过 20000 字符')
    if not 4 <= request['duration'] <= max_duration:
        raise ValueError(f'该模型时长范围为 4–{max_duration} 秒')
    for key in ('image_urls', 'video_urls', 'audio_urls'):
        values = payload.get(key) or []
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError(f'{key} 必须为 URL 数组')
        request[key] = list(dict.fromkeys(public_media_url(v) for v in values))
    for key in ('first_frame', 'last_frame'):
        value = payload.get(key) or payload.get(key + '_url')
        if value:
            request[key] = public_media_url(value)
    if request.get('last_frame') and not request.get('first_frame'):
        raise ValueError('last_frame 必须与 first_frame 一起使用')
    for key in ('generate_audio', 'seed', 'fps'):
        if key in payload and payload[key] is not None:
            request[key] = payload[key]
    return request


def at(value, path, default=None):
    if not path:
        return value
    for key in path.split('.'):
        try:
            value = value[int(key)] if isinstance(value, list) else value[key]
        except (KeyError, IndexError, TypeError, ValueError):
            return default
    return value


def put(value, path, item):
    parts = path.split('.')
    for part in parts[:-1]:
        value = value.setdefault(part, {})
    value[parts[-1]] = item


def validate_profiles(profiles, tools):
    if not isinstance(profiles, dict):
        raise ValueError('模型配置必须为 JSON 对象')
    by_name = {tool['name']: tool for tool in tools}
    for public, profile in profiles.items():
        if public not in PUBLIC_MODELS or not isinstance(profile, dict):
            raise ValueError(f'不支持的对外模型：{public}')
        for key in ('submit_tool', 'status_tool', 'upstream_model', 'status_id_parameter'):
            if not isinstance(profile.get(key), str) or not profile[key].strip():
                raise ValueError(f'{public}: 缺少 {key}')
        for key in ('submit_tool', 'status_tool'):
            if profile[key] not in by_name:
                raise ValueError(f'{public}: 工具未出现在该账号的实时 tools/list 中：{profile[key]}')
        mapping = profile.get('parameters')
        if not isinstance(mapping, dict) or not {'model', 'prompt', 'duration', 'resolution', 'aspect_ratio'} <= mapping.keys():
            raise ValueError(f'{public}: parameters 必须明确映射 model/prompt/duration/resolution/aspect_ratio')
        if not mapping.keys() <= INPUT_FIELDS or any(not isinstance(v, str) or not v for v in mapping.values()):
            raise ValueError(f'{public}: parameters 包含未知或空字段')
        constraints = profile.get('constraints', {})
        for key in ('durations', 'resolutions', 'aspect_ratios'):
            if not isinstance(constraints.get(key), list) or not constraints[key]:
                raise ValueError(f'{public}: 必须根据上游能力填写 constraints.{key}')
        for key in ('max_images', 'max_videos', 'max_audios'):
            value = constraints.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f'{public}: 必须填写非负整数 constraints.{key}')
        Draft202012Validator.check_schema(local_schema(by_name[profile['submit_tool']].get('inputSchema', {})))
        Draft202012Validator.check_schema(local_schema(by_name[profile['status_tool']].get('inputSchema', {})))
    return profiles


def build_arguments(request, profile, tools):
    constraints = profile['constraints']
    for field, allowed in [('duration', 'durations'), ('resolution', 'resolutions'), ('aspect_ratio', 'aspect_ratios')]:
        if request[field] not in constraints[allowed]:
            raise ValueError(f'Artlist 模型不支持该 {field}')
    frames = [request[k] for k in ('first_frame', 'last_frame') if request.get(k)]
    counts = {'max_images': len(set(request['image_urls'] + frames)), 'max_videos': len(request['video_urls']), 'max_audios': len(request['audio_urls'])}
    for key, count in counts.items():
        if count > constraints[key]:
            raise ValueError(f'Artlist 模型素材数量超限：{key}')
    arguments = dict(profile.get('constants', {}))
    values = {**request, 'model': profile['upstream_model']}
    for source, value in values.items():
        if value in (None, [], ''):
            continue
        target = profile['parameters'].get(source)
        if not target:
            raise ValueError(f'Artlist 工具未配置 {source}，不能丢弃该请求参数')
        converted = profile.get('value_map', {}).get(source, {}).get(str(value), value)
        put(arguments, target, converted)
    tool = next((tool for tool in tools if tool['name'] == profile['submit_tool']), None)
    if not tool:
        raise ValueError('模型提交工具已不可用，请重新检测账号')
    try:
        Draft202012Validator(local_schema(tool.get('inputSchema', {}))).validate(arguments)
    except ValidationError as exc:
        raise ValueError(f'请求不符合 Artlist 工具参数定义：{exc.message}') from None
    return arguments


def query_arguments(profile, upstream_id, tools):
    arguments = dict(profile.get('status_constants', {}))
    put(arguments, profile['status_id_parameter'], upstream_id)
    tool = next((tool for tool in tools if tool['name'] == profile['status_tool']), None)
    if not tool:
        raise ValueError('状态查询工具已不可用')
    Draft202012Validator(local_schema(tool.get('inputSchema', {}))).validate(arguments)
    return arguments


def task_identity(data, profile):
    if profile.get('id_path'):
        value = at(data, profile['id_path'])
    else:
        value = next((at(data, key) for key in ('generation_id', 'generationId', 'task_id', 'taskId', 'id', 'data.generation_id', 'data.generationId', 'data.id') if at(data, key)), None)
    return str(value) if isinstance(value, (str, int)) and value else ''


def task_result(data, profile):
    state = str(at(data, profile.get('status_path', 'status'), '') or at(data, 'data.status', '')).lower()
    if state in {'failed', 'error', 'cancelled', 'canceled', 'rejected'}:
        return 'failed', ''
    value = at(data, profile['video_url_path']) if profile.get('video_url_path') else None
    if not value:
        value = next((at(data, key) for key in ('video_url', 'videoUrl', 'content.video_url', 'data.video_url', 'data.videoUrl', 'output.video_url', 'outputs.0.video_url', 'download_url') if at(data, key)), None)
    if isinstance(value, str) and value.startswith('https://'):
        if state not in {'queued', 'pending', 'running', 'processing', 'in_progress'}:
            return 'succeeded', public_media_url(value)
    if state in {'completed', 'succeeded', 'success', 'done', 'finished'}:
        raise ValueError('Artlist 已完成，但未解析到视频 URL；请核对 video_url_path')
    if not state:
        raise ValueError('Artlist 状态响应缺少可识别状态；请核对 status_path')
    return 'running', ''


def profile_template():
    return {'submit_tool': '', 'status_tool': '', 'upstream_model': '', 'status_id_parameter': '',
            'parameters': {'model': 'model', 'prompt': 'prompt', 'duration': 'duration', 'resolution': 'resolution', 'aspect_ratio': 'aspect_ratio'},
            'constraints': {'durations': [], 'resolutions': [], 'aspect_ratios': [], 'max_images': 0, 'max_videos': 0, 'max_audios': 0},
            'id_path': '', 'status_path': 'status', 'video_url_path': ''}
