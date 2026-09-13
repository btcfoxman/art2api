"""Caller-visible error vocabulary; provider diagnostics stay in admin storage."""
from __future__ import annotations


QUEUE_LIMIT = '上游队列排队限额，请稍后再试~'
CONTENT_POLICY = '检测到内容有敏感或违规情况，请修改后重试，积分已返还～'
REFERENCE_PERSON = '参考图片中检测到可能存在真人，暂不支持，请更换图片后重试，积分已返还~'
INPUT_PERSON = '检测到输入图片可能包含真人，生成失败，请修改后重试，积分已返还~'
OUTPUT_VIDEO_POLICY = '生成的视频内容违规，请修改描述后重试，积分已返还~'
TEXT_RETRY = '文字违规！请重试，积分已返还～'
VIDEO_RETRY = '视频违规！请重试，积分已返还～'
TEXT_EDIT = '文字违规！请修改后重试，积分已返还～'
VIDEO_POLICY = '检测到视频有敏感或违规内容，请修改后重试，积分已返还～'
IMAGE_POLICY = '检测到图片有敏感或违规内容，积分已返还，请重试~'
CONTENT_RETRY = '检测到内容有敏感或违规情况，积分已返还，请重试~'
IMAGE_EDIT = '图片违规，请修改后重试~'
TEXT_POLICY = '检测到文本有敏感或违规内容，积分已返还，请重试~'
QUEUE_INTERRUPTED = '队列排队服务中断，请稍后再试~'
MEDIA_DURATION = '素材时长不支持，请修改后再试~'
MEDIA_LIMIT = '素材超限，请修改后再试~'
MEDIA_FORMAT = '素材格式不支持，请修改后再试~'
MEDIA_DOWNLOAD = '素材下载失败，请检查素材链接后重试~'
MEDIA_EXTERNAL = '素材仅支持外链，暂不支持文件流/Base64等~'
GENERATION_FAILED = '生成失败，积分已返还，请重试~'

PUBLIC_MESSAGES = frozenset({
    QUEUE_LIMIT, CONTENT_POLICY, REFERENCE_PERSON, INPUT_PERSON, OUTPUT_VIDEO_POLICY,
    TEXT_RETRY, VIDEO_RETRY, TEXT_EDIT, VIDEO_POLICY, IMAGE_POLICY, CONTENT_RETRY,
    IMAGE_EDIT, TEXT_POLICY, QUEUE_INTERRUPTED, MEDIA_DURATION, MEDIA_LIMIT,
    MEDIA_FORMAT, MEDIA_DOWNLOAD, MEDIA_EXTERNAL, GENERATION_FAILED,
})

# These generic codes carry routing/idempotency semantics. Never echo arbitrary
# upstream codes, tool names, or exception text in their place.
PUBLIC_CODES = frozenset({
    'generation_failed', 'entitlement_unavailable', 'submission_unknown',
    'upstream_outcome_unknown', 'idempotency_conflict', 'validation_error',
    'not_found', 'conflict', 'unauthorized', 'forbidden', 'rate_limited',
    'method_not_allowed', 'internal_error', 'upstream_error',
})


def public_message(code='', message='', upstream_code=''):
    if code in {'submission_unknown', 'upstream_outcome_unknown'}:
        # Unknown submission outcomes must not suggest a confirmed refund.
        return QUEUE_INTERRUPTED
    if message in PUBLIC_MESSAGES:
        return message
    text = f'{upstream_code} {message}'.lower()
    if code in {'entitlement_unavailable', 'rate_limited'} or any(word in text for word in (
        'insufficient_credits', '积分不足', '额度', '并发', 'queue is full', 'rate limit', '排队限额',
    )):
        return QUEUE_LIMIT
    if code in {'unauthorized', 'forbidden', 'internal_error', 'proxy_error',
                'task_timeout', 'reauthorization_required', 'verification_required',
                'verification_unavailable', 'web_upstream_error', 'mcp_transport_error',
                'browser_error', 'web_protocol_error', 'mcp_protocol_error'}:
        return QUEUE_INTERRUPTED
    # Only inspect diagnostics to choose a fixed literal, never to build one.
    if any(word in text for word in ('real person', 'real_person', 'realperson', '真人', 'human face')):
        return INPUT_PERSON if any(word in text for word in ('input image', 'inputimage', '输入图片')) else REFERENCE_PERSON
    if any(word in text for word in ('sensitive', 'copyright', 'content_safety', '违规', '审核', '版权', 'moderation')):
        if any(word in text for word in ('outputvideo', 'output video', '输出视频', '生成的视频')):
            return OUTPUT_VIDEO_POLICY
        if any(word in text for word in ('inputtext', 'input text', 'inputprompt', 'input prompt', '文本', '文字', '提示词审核')):
            return TEXT_POLICY
        if any(word in text for word in ('inputimage', 'input image', 'reference image', '图片', '图像')):
            return IMAGE_POLICY
        if any(word in text for word in ('inputvideo', 'input video', 'reference video', '视频')):
            return VIDEO_POLICY
        return CONTENT_POLICY
    if any(word in text for word in ('时长', 'duration')):
        return MEDIA_DURATION
    if any(word in text for word in ('素材数量', '素材总数', '素材总量', '素材文件大小', '素材大小', '素材总限制',
                                     'image_urls 数量超限', 'video_urls 数量超限', 'audio_urls 数量超限', '素材总数不能超过',
                                     'too many images', 'too many videos', 'too many audios', 'file size', 'size limit')):
        return MEDIA_LIMIT
    if any(word in text for word in ('公网 https url', 'url 数组', 'base64', '文件流', '仅支持外链')):
        return MEDIA_EXTERNAL
    if code in {'media_unreachable', 'media_signature_invalid'} or any(word in text for word in (
        'input_url_unreachable', '素材下载', '素材地址', '素材不可读取', '无法读取参考素材', 'for url',
    )):
        return MEDIA_DOWNLOAD
    if any(word in text for word in ('素材格式', '帧率', '图片尺寸', '素材转换失败', '素材信息解析',
                                     '不支持 image_urls', '不支持 video_urls', '不支持 audio_urls')):
        return MEDIA_FORMAT
    return GENERATION_FAILED


def public_error(code, message='', *, upstream_code='', stored_message=''):
    selected = stored_message if stored_message in PUBLIC_MESSAGES else message
    return {'code': code if code in PUBLIC_CODES else 'upstream_error',
            'message': public_message(code, selected, upstream_code)}
