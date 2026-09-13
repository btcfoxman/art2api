# ART2API

独立的 Artlist Seedance 视频渠道，供 lingya2api 的 ARTAPI 卡片调用。默认采用根据用户 CDP 操作记录实现的网页协议；保留可选 MCP 后端。每个账号独立绑定固定代理、会话与任务，代理失败不会直连。

## 配置与部署

`pre` 分支推送触发 `.github/workflows/deploy-test.yml`：测试 → 构建并推送 GHCR 镜像 → `art2api-pre` 专用 runner 部署 → 检查 cloudflared 和公开 HTTPS。

- 控制台：<https://art2api.aiid.edu.kg>
- 预发布服务器：192.168.3.5；服务端口：8797。
- 服务目录：`/home/btcfoxman/docker/art2api`；Docker 网络：`my-shared-net`。
- 数据挂载：`./data:/app/data`。数据库、浏览器 Profile 与 `.env` 均为私有运行数据，备份时保留对应加密密钥。

复制 `.env.example` 为 `.env`，配置相互独立的 `ART_API_KEY`、`ART_ADMIN_TOKEN`（各至少 24 字符）和 Fernet `ART_ENCRYPTION_KEY`。运行 `docker compose up -d`。镜像选择由 workflow 的 `IMAGE_*` 临时环境变量传给 Compose，不写入应用 `.env`。

管理登录使用 `ART_ADMIN_TOKEN`。公网可访问 `https://art2api.aiid.edu.kg/login`，内网可访问 `http://192.168.3.5:8797/login`。会话 Cookie 根据访问地址设置：内网 HTTP 可正常保留登录，HTTPS 及经 cloudflared 转发的公网域名保持 `Secure`。`ART_PUBLIC_BASE_URL` 保留公网 HTTPS 地址，无须为内网访问修改。

本地开发安装 `requirements.txt`，并准备 Chromium/Chrome 与 PATH 中的 `ffprobe`。设置 `ART_CHROME_EXECUTABLE` 后运行 `uvicorn main:app --host 127.0.0.1 --port 8797`。浏览器使用原生 CDP WebSocket，没有 Playwright 依赖。Docker 镜像包含 Chromium、Xvfb、ffmpeg 和中文字体。默认在独立虚拟显示器中运行完整图形浏览器；`ART_BROWSER_HEADLESS=1` 可切换无头模式，但正常验证 SDK 可能拒绝该环境。

## 账号卡片

1. 使用管理密钥登录，添加账号，选择“Artlist 网页”，填写固定代理和最大并发。
2. 点击“连接账号”，通过该账号代理完成正常网页登录，再点击“保存网页登录”。也可以在“网页登录设置”导入自己同一固定出口下的 Cookie、User-Agent 和可选团队 ID。
3. “检测连接”核对代理出口、网页登录身份和模型目录，自动加载 10 个模型配置，再启用账号。
4. 卡片展示接入方式、代理地址、检测出口、登录状态、提交验证状态、模型数量和运行任务。多个账号使用独立代理和浏览器 Profile。
5. “查询网页任务”接受该账号已有的 Artlist generation ID，直接核对任务状态和输出文件，无须重复生成。

生成和查询通过固定代理发送 HTTP 协议请求。提交前服务端用该账号的原生 CDP 浏览器运行正常验证 SDK，不操作模型选择或生成按钮，并阻止浏览器发送生成请求。SDK 成功时携带一次性 `turnstileToken`；失败时按照网页客户端协议携带 SDK 实际返回的 `turnstileClientError`，是否接受由 Artlist 决定。需要交互验证时明确报错，不自动解题。该流程仍依赖后台 Chromium，不能称为完全无浏览器。保留可选的一次性验证令牌输入，仅供一个任务使用。明确拒绝会保存错误并交给渠道 fallback；提交结果未知时禁止重提。

Cookie、OAuth 令牌、代理认证和网页验证令牌均加密保存，不通过账号查询接口回传。浏览器 Profile 也应作为敏感数据保护。代理支持 HTTP/SOCKS5，`xray:20001` 会规范化为 SOCKS5。CDP 登录浏览器需要无认证的本地代理入口，例如 `socks5://xray:20001`；协议调用支持带认证代理。账号有未结束任务时不能更换代理或切换接入方式；重新登录恢复查询时必须保持同一 Artlist 身份。

MCP 为可选后端，地址固定为 `https://mcp.artlist.io/mcp`。该模式仍需要 Artlist 接受的 OAuth Client ID，通过 `ART_OAUTH_CLIENT_ID` 配置，并将回调注册为 `ART_PUBLIC_BASE_URL/oauth/callback`。网页后端不需要 OAuth Client ID。MCP 账号的模型映射根据实际 `tools/list` 手动配置；网页账号自动加载内置映射。

## 模型与验证范围

| 对外模型 | Artlist 模型组 | 默认/固定分辨率 | 当前验证 |
|---|---|---|---|
| `doubao-seedance-2-0-fast-260128` | Seedance 2.0 Fast / 377 | 默认 720p | 实时报价 |
| `doubao-seedance-2-0-260128` | Seedance 2.0 / 358 | 默认 720p | 实时报价 |
| `doubao-seedance-2-0-260128-4k` | Seedance 2.0 / 358 | 固定 4k | 实时报价 |
| `doubao-seedance-2-0-mini-260615` | Seedance 2.0 Mini / 416 | 默认 720p | 720p 渠道协议生成与结果查询 |
| `doubao-seedance-2-0-fast-260128-480p` | Seedance 2.0 Fast / 377 | 固定 480p | 渠道协议生成与结果查询 |
| `doubao-seedance-2-0-260128-480p` | Seedance 2.0 / 358 | 固定 480p | 实时报价 |
| `doubao-seedance-2-0-260128-1080p` | Seedance 2.0 / 358 | 固定 1080p | 实时报价 |
| `sd-2-5` | Seedance 2.5 / 515 | 固定 720p | 实时报价 |
| `sd-2-5-480p` | Seedance 2.5 / 515 | 固定 480p | 用户实际生成与结果查询 |
| `sd-2-5-1080p` | Seedance 2.5 / 515 | 固定 1080p | 实时报价 |

支持文本、参考图片/视频/音频以及首尾帧参数映射。每次提交重新报价，按报价返回的子模型 ID 和实时 JSON Schema 校验该组合，不硬编码子模型、积分或报价签名。未实操组合根据捕获的模型配置推断实现，仍需实际生成验收；并非所有型号都支持所有素材组合。

素材按采集的网页流程上传：先使用 PUT 签名上传，再通过 `uploadRouter.getPresignedUrlFromKey` 获取独立 GET 签名。报价、生成输入和 artifact 均保留完整下载签名；提交前通过账号固定代理验证素材可读取，避免私有 S3 原始地址返回 403 后仍进入付费生成。上游失败时保留 `INPUT_URL_UNREACHABLE` 等结构化错误码，错误消息不包含签名 URL。

多参考素材提示词中的 `@参考1` / `@图片1`、`@视频1`、`@音频1` 会按原素材序号转换为 Artlist 的 `@img1`、`@vid1`、`@aud1`。仅为提示词中实际使用的引用生成 `tagReferences`，无引用标签的普通图生视频仍保留参考图片；不存在的素材序号在提交前报错。

2.0/Fast/Mini 的时长为 4–15 秒，2.5 为 4–30 秒；分辨率和比例按模型配置校验。固定分辨率别名锁定分辨率。不支持的参数和超出上游限制的素材明确拒绝，不丢弃素材或自动转换成其他型号。`seed`、`fps` 尚未在采集中确认，网页后端拒绝这些生成参数。

## lingya2api 设置

在 ARTAPI 卡片配置：

- Base URL：共享 Docker 网络使用 `http://art2api:8797`，外部调用使用公开 HTTPS 地址。
- API Key：本服务的 `ART_API_KEY`。
- Model Map：上表 10 个模型名使用同名映射；显式保存空对象会使该渠道没有可路由模型。
- 开关：启用或关闭 ARTAPI。`Channel Routing Rules` 中的 `artapi` 位置决定首选与 fallback 顺序。

Artlist 账号、会话和固定代理在 art2api 的账号卡片维护；lingya2api 卡片维护服务地址、服务密钥、模型映射与路由。账号关闭或并发已满时不接受新任务，已有生成继续使用原账号、原代理查询。

## API

业务接口使用 `Authorization: Bearer <ART_API_KEY>` 或 `X-API-Key`；管理接口使用独立登录会话。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 健康与版本 |
| GET | `/v1/models` | 已启用账号的模型能力，含 `verification_required` 与验证范围 |
| POST | `/v1/videos` | 创建异步任务 |
| GET | `/v1/videos/{id}` | 查询本地任务 |
| POST | `/api/v3/contents/generations/tasks` | lingya2api 兼容提交 |
| GET | `/api/v3/contents/generations/tasks/{id}` | lingya2api 兼容查询 |
| PUT | `/api/accounts/{id}/web-session` | 管理端导入 Cookie/User-Agent/可选 team_id |
| POST | `/api/accounts/{id}/web-verification` | 管理端登记一次性验证令牌 |
| POST | `/api/accounts/{id}/web-quote` | 管理端无素材报价检测，不生成 |
| GET | `/api/accounts/{id}/web-tasks/{generation_id}` | 管理端查询已有网页任务与输出 |

```json
{"model":"sd-2-5-480p","prompt":"海边日出，镜头缓慢推进","duration":5,"aspect_ratio":"16:9","image_urls":[],"video_urls":[],"audio_urls":[],"generate_audio":true}
```

可选通过请求的 `verification_account_id` 和 `verification_token` 绑定账号并登记当次验证，或预先在管理端登记；常规协议调用不需要这两个字段。令牌不写入任务请求内容。推荐使用 `Idempotency-Key`：同一键与同一规范化请求返回原任务，不会再次提交；相同键与不同请求返回 409。

网页任务先用 generation ID 查询 `getUserGenerationById`，再用 output ID 查询 `getUserGenerationOutputById`，核对两者关联后返回视频地址。进程重启恢复原任务查询。提交中断或接受情况不明时标记 `submission_unknown`，阻止重复扣费与自动 fallback；管理员可补充真实 generation ID 恢复查询。完成状态暂时没有输出时继续查询，低于请求分辨率的输出不作为成功结果返回。

当前使用单个 Uvicorn 进程，不支持多个进程共同调度同一 SQLite 数据库。

## 验证

运行 `python -m pytest tests -q`。测试覆盖固定代理、会话加密、模型映射、素材参数、报价/提交字段差异、任务与输出 ID 校验、一次性验证、幂等和未知提交恢复，以及 Cookie 中的 JSON 值、登录域、匿名会话保护、容器更换后的 Profile 锁和浏览器正常退出。

2026-09-13 在 192.168.3.5 pre 中，使用已部署 lingya2api 的 ARTAPI 提交和查询适配器，连续调用已部署 art2api，完成以下两条真实生成。没有向请求预先注入人工提供的验证令牌，未点击 Artlist 模型或生成按钮；验证由服务端 Chromium 的正常 SDK 完成，提交和查询使用 HTTP 协议。

| 请求 | 最终视频 | 音频 | 结果 |
|---|---|---|---|
| Seedance 2.0 Mini，720p，4 秒 | 1280×720，MP4 容器 4.096 秒 | 有 | 生成、查询、下载与 ffprobe 检查通过 |
| Seedance 2.0 Fast，480p，4 秒 | 864×496，MP4 容器 4.096 秒 | 有 | 生成、查询、下载与 ffprobe 检查通过 |

全部 10 个模型的文本报价与子模型定义此前已验证，另已查询确认用户创建的 Seedance 2.5 480p 任务完成。其余分辨率及推断参数组合尚未逐项生成验收，不能将报价通过视为生成通过。
