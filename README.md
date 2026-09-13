# ART2API

Artlist MCP Seedance 视频网关。独立账号、强制固定代理、OAuth 授权、持久化任务，供 lingya2api 的 ARTAPI 渠道调用。

## 部署

参考 ak2api：`pre` 分支推送触发 `.github/workflows/deploy-test.yml`，先运行测试，再构建 GHCR 镜像，交给 `art2api-pre` 专用 runner 部署。

- 服务端口：8797
- 预发布服务器：192.168.3.5
- 服务目录：`/home/btcfoxman/docker/art2api`
- 公开控制台：`https://art2api.aiid.edu.kg`
- Docker 网络：`my-shared-net`
- 数据挂载：`./data:/app/data`，必须与 `.env` 中的加密密钥一起备份。

将 `.env.example` 复制为 `.env`，生成相互独立的 `ART_API_KEY`、`ART_ADMIN_TOKEN`（至少 24 字符）和 Fernet `ART_ENCRYPTION_KEY`。例如使用 Python `secrets.token_urlsafe(32)` 和 `cryptography.fernet.Fernet.generate_key()`。不要将生成值提交到 Git。

镜像选择由工作流通过 `IMAGE_REGISTRY`、`IMAGE_NAMESPACE`、`IMAGE_NAME`、`IMAGE_TAG` 临时环境变量传给 Compose；应用 `.env` 不保存 `IMAGE_*`。

运行：`docker compose up -d`。本地开发安装 `requirements.txt`，设置 `ART_*` 环境变量后运行 `uvicorn main:app --host 127.0.0.1 --port 8797`。本地浏览器运行需要 `playwright install chromium` 或指定 `ART_CHROME_EXECUTABLE`。

## 账号配置

1. 使用管理密钥登录，添加账号名称、固定代理地址和并发数。
2. 代理必填，支持 `http://user:password@host:port`、`socks5://host:port`，`xray:20001` 自动转换为 SOCKS5。不存在直连或环境代理继承路径。
3. 点击“连接账号”。服务端为该账号启动独立浏览器 Profile，浏览器、OAuth 和 MCP 使用同一个代理。通过画面和输入栏完成正常 Artlist 登录；出现人工验证时在该窗口完成。
4. 点击“检测连接”，读取出口 IP 与真实 MCP 工具定义。
5. 在“模型配置”中读取上游模型目录，登记支持的 Seedance 模型、参数映射、输出路径和能力限制，再启用账号。

不存储 Artlist 登录密码。OAuth 凭据和代理 URL 使用 Fernet 加密保存；浏览器 Profile 包含会话信息，属于敏感运行数据。数据库、Profile 和环境文件均不提交 Git。

代理不通不会回退直连。多个账号检测到同一出口时不会接新任务。出口检测仅为最近检测快照；固定独立 IP 需要代理服务保证。Chromium 授权支持 HTTP 代理认证或无认证 SOCKS5；带认证 SOCKS5 请提供相同出口的 HTTP 接口。

Artlist 当前公布动态注册地址但实际禁用了动态注册。网关优先使用其声明支持的 OAuth Client ID Metadata Document，公开地址位于 `/oauth/client-metadata.json`。亦可通过 `ART_OAUTH_CLIENT_ID` 使用合法预注册客户端；回调必须注册为 `ART_PUBLIC_BASE_URL/oauth/callback`。

## 模型能力

首版只暴露 lingya2api 已有的 Seedance 2.0、Fast、Mini、固定分辨率别名，以及 `sd-2-5`、`sd-2-5-480p`、`sd-2-5-1080p`。某个对外名字已登记，不代表 Artlist 账号有该能力；只有该账号实时工具列表和显式模型配置匹配的请求才可提交。

模型配置以对外模型名为键。每个值需要：

- `submit_tool`、`status_tool`：真实 `tools/list` 返回的工具名称。
- `upstream_model`：模型查询工具返回的实际模型 ID。
- `parameters`：对外参数到工具入参的映射，支持点分嵌套路径。
- `constraints`：`durations`、`resolutions`、`aspect_ratios`，以及 `max_images`、`max_videos`、`max_audios`。
- `status_id_parameter`：查询工具接收生成 ID 的字段。
- 可选 `id_path`、`status_path`、`video_url_path`：结构化响应中的字段路径。
- 可选 `constants`、`status_constants`、`value_map`：上游必需常量和枚举值转换。

请求会同时经过模型能力和真实 JSON Schema 校验，不会静默删除素材、改变时长或降低分辨率。不自动将 Fast、Mini 或 2.5 替换为标准模型。

## 对外接口

所有业务接口使用 `Authorization: Bearer <ART_API_KEY>` 或 `X-API-Key`。管理接口仅接受管理登录会话，两者隔离。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 进程健康 |
| GET | `/v1/models` | 当前可接单的模型与能力 |
| POST | `/v1/videos` | 提交异步任务 |
| GET | `/v1/videos/{id}` | 查询任务 |
| POST | `/api/v3/contents/generations/tasks` | lingya2api 兼容提交 |
| GET | `/api/v3/contents/generations/tasks/{id}` | lingya2api 兼容查询 |

```json
{"model":"doubao-seedance-2-0-260128","prompt":"海边日出，镜头缓慢推进","duration":5,"resolution":"720p","aspect_ratio":"16:9","image_urls":[]}
```

推荐传 `Idempotency-Key`。同一键与同一规范化请求返回原任务；相同键与不同请求返回 409。任务提交前即持久化账号和代理版本；查询、重启恢复不切换账号。

上游提交中断、缺少生成 ID、查询结果不明会保留 `submission_unknown` 状态，不重新提交。对外返回 `failed` 并携带 `submission_unknown` 或 `upstream_outcome_unknown` 错误码，lingya2api 据此停止自动渠道切换。管理员可填入真实上游生成 ID 恢复查询。

没有可用账号或兼容能力时返回 HTTP 503 / `entitlement_unavailable`，明确表示本次没有提交生成。Artlist 明确失败才按普通失败交给渠道 fallback。

当前部署使用单 Uvicorn 进程。不要让多个服务进程共同使用同一数据库。关闭渠道或账号只停止新提交，不取消 Artlist 已开始的生成。

## 验证

`python -m pytest tests -q`

核心测试覆盖强制代理、密钥隔离、加密持久化、幂等性、账号与代理绑定、重复出口、能力校验和未知提交结果不重提。真实工具定义、授权及生成验收另在预发布部署执行。
