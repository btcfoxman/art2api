# API

业务接口使用 `Authorization: Bearer <ART_API_KEY>` 或 `X-API-Key`；管理接口使用独立登录会话。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 健康与版本 |
| GET | `/v1/models` | 已启用账号的模型能力，含 `verification_required` 与验证范围 |
| POST | `/v1/videos` | 创建异步任务 |
| GET | `/v1/videos/{id}` | 查询本地任务 |
| POST | `/api/v3/contents/generations/tasks` | lingya2api 兼容提交 |
| GET | `/api/v3/contents/generations/tasks/{id}` | lingya2api 兼容查询 |
| POST / PATCH | `/api/accounts` / `/api/accounts/{id}` | 管理端创建或编辑账号；`max_concurrency` 为正整数，不设 20 的上限 |
| PUT | `/api/accounts/{id}/web-session` | 管理端导入 Cookie/User-Agent/可选 team_id |
| POST | `/api/accounts/{id}/web-verification` | 管理端登记一次性验证令牌 |
| POST | `/api/accounts/{id}/web-quote` | 管理端无素材报价检测，不生成 |
| GET | `/api/accounts/{id}/web-tasks/{generation_id}` | 管理端查询已有网页任务与输出 |
| DELETE | `/api/tasks/completed` | 管理端清空最近任务列表中所有已成功或已失败的任务；保留进行中和结果未知任务 |

```json
{"model":"sd-2-5-480p","prompt":"海边日出，镜头缓慢推进","duration":5,"aspect_ratio":"16:9","image_urls":[],"video_urls":[],"audio_urls":[],"generate_audio":true}
```

可选通过请求的 `verification_account_id` 和 `verification_token` 绑定账号并登记当次验证，或预先在管理端登记；常规协议调用不需要这两个字段。令牌不写入任务请求内容。推荐使用 `Idempotency-Key`：同一键与同一规范化请求返回原任务，不会再次提交；相同键与不同请求返回 409。

网页任务先用 generation ID 查询 `getUserGenerationById`，再用 output ID 查询 `getUserGenerationOutputById`，核对两者关联后返回视频地址。进程重启恢复原任务查询。提交中断或接受情况不明时标记 `submission_unknown`，阻止重复扣费与自动 fallback；管理员可补充真实 generation ID 恢复查询。完成状态暂时没有输出时继续查询，低于请求分辨率的输出不作为成功结果返回。

当前使用单个 Uvicorn 进程，不支持多个进程共同调度同一 SQLite 数据库。

清空操作需要管理登录会话及 `X-Requested-With: art2api`，返回清理条数（如 `{"cleared":12}`），覆盖所有分页。清空后历史任务仍可按 ID 查询，幂等记录保持有效，不会因清空而重复生成。

导入网页登录会话时 `team_id` 可留空。创建会话会按网页协议自动生成 UUID 填入必需的 `teamId`，无需查询或填写账号团队信息；已有显式配置仍保留兼容。

任务仍受运行设置中的全局在途任务上限及上游实际额度约束。素材下载与上传均走所属账号的固定代理；每账号最多同时处理 3 份素材，按原请求顺序提交。管理端任务详情包含素材分项耗时和协议请求等待时间，业务查询接口不返回内部诊断信息。
