from pydantic import BaseModel, ConfigDict, Field


class RuntimePatch(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_timeout_seconds: int | None = Field(default=None, ge=10, le=600)
    poll_interval_seconds: int | None = Field(default=None, ge=1, le=300)
    task_timeout_seconds: int | None = Field(default=None, ge=60, le=86400)
    queue_limit: int | None = Field(default=None, ge=1, le=10000)
    browser_timeout_seconds: int | None = Field(default=None, ge=60, le=7200)
    browser_headless: bool | None = None


FIELDS = {
    'request_timeout_seconds': 'request_timeout',
    'poll_interval_seconds': 'poll_interval',
    'task_timeout_seconds': 'task_timeout',
    'queue_limit': 'queue_limit',
    'browser_timeout_seconds': 'browser_timeout',
    'browser_headless': 'browser_headless',
}


def apply_runtime(settings, values):
    values = RuntimePatch.model_validate(values).model_dump(exclude_none=True)
    for key, value in values.items():
        setattr(settings, FIELDS[key], value)


def runtime_values(settings):
    return {key: getattr(settings, attr) for key, attr in FIELDS.items()}
