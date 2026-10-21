# hajimi 补丁版：部署到 Render + 接入 DSH

这个目录是 `wyeeeee/hajimi` 的源码副本，加了一组针对 **DSH 带工具调用报 400** 的修复和几项实用功能。
适合：Render 免费层部署一个 OpenAI 兼容的 Gemini 代理，然后在 DeepSeek Harness 里当自定义模型用。

## 一、修了什么 / 加了什么

| 改动 | 说明 |
| --- | --- |
| `app/utils/gemini_schema.py`（新增） | 把 OpenAI 的 tools JSON Schema 白名单化成 Gemini 能接受的形状：去掉 `additionalProperties`、`$schema`、`const`、`propertyOrdering` 等非法字段；`oneOf` 转 `anyOf`，`{"type":"null"}` 分支合并成 `nullable`；数组型 `type` 压成单类型 + `nullable`；非字符串枚举丢弃。**这是 DSH 报 400 的根因修复。** |
| `app/services/gemini.py` | 调用上面前先清洗 `parameters`；带 tools 时不再注入随机串（原来会把工具参数污染成 `/etc/hostsjZItAcc13H`）；`tool_choice="required"` 映射为 Gemini 的 `mode=ANY`。 |
| `app/models/schemas.py` | `tool_choice` 允许 `"required"`。 |
| `app/utils/error_handling.py` + 6 处失败文案 | 记住最近一次上游真实错误，失败响应里追加 `最近一次上游错误: ...`。以前上游 400 被吞成「所有API密钥均请求失败」，根本没法排障。可用 `SHOW_UPSTREAM_ERROR=false` 关掉。 |
| `app/utils/response.py` | tool_calls 补 `id` 和流式 `index`，修复多工具调用时客户端合并错乱。 |
| `app/api/routes.py` | 新增免鉴权 `GET /healthz`（不访问上游，给 Render 健康检查/保活用）；新增 `GET /v1/models/{model_id}`。 |
| `app/utils/key_scheduler.py`（新增） | 健康感知的密钥调度：按「在途请求数 → 连续失败数 → 最久未使用」挑 key；key 无效直接下线；429 限流指数退避（遵守 `Retry-After`）；反复配额超限则冷却到太平洋时间零点；5xx/网络错误短暂冷却；全部 key 都在冷却时 fail-open 选最早恢复的。 |
| `app/utils/api_key.py` | 轮询从原来的「随机洗牌栈」换成上面的调度器，对外 API 不变（`get_available_key`）。 |
| `app/api/stream_handlers.py`、`app/api/nonstream_handlers.py` | 每个请求结束上报成功/失败并释放并发计数，让调度器有真实健康数据。 |
| `app/api/routes.py` | 另加 `GET /v1/keys`（查看每个 key 的状态，需密码）和 `POST /v1/keys/reset`（手动恢复冷却/失效的 key）。 |
| `Dockerfile` | 端口改为 `${PORT:-7860}`，Render 注入的 `PORT` 才能生效。 |

## 二、部署到 Render（免费层）

### 1. 推到 GitHub

```bash
cd hajimi-fix
git init && git add -A && git commit -m "hajimi: fix gemini tool schema + add healthz"
# 在 GitHub 新建空仓库后：
git remote add origin https://github.com/<你的用户名>/hajimi-patched.git
git push -u origin main
```

不想用命令行也行：GitHub 网页 → New repository → 上传本目录全部文件（别传 `.env`，密钥用 Render 环境变量配）。

### 2. Render 建服务

方式 A（推荐，仓库里已带 `render.yaml`）：Render 控制台 → **New → Blueprint** → 选这个仓库 → 会按 `render.yaml` 自动创建 Web Service（Docker、Free、健康检查 `/healthz`），填两个 secret 即可。

方式 B（手动）：**New → Web Service** → 选仓库 → Language 选 **Docker** → Instance Type 选 **Free** → Health Check Path 填 `/healthz` → 环境变量如下：

| Key | Value |
| --- | --- |
| `PASSWORD` | 你的访问密码（客户端 API Key 就是它） |
| `GEMINI_API_KEYS` | 你的 Gemini API Key，多个用英文逗号分隔 |
| `FAKE_STREAMING` | `false`（真流式，DSH 体验更好） |
| `API_KEY_DAILY_LIMIT` | `1000` |
| `KEY_COOLDOWN_BASE` | `15` |
| `KEY_COOLDOWN_MAX` | `300` |
| `CACHE_EXPIRY_TIME` | `60` |
| `ENABLE_STORAGE` | `false` |
| `SHOW_UPSTREAM_ERROR` | `true`（可选，默认开） |

不要手动设 `PORT`，Render 会注入，Dockerfile 已读取它。

### 3. 验证

```bash
curl -s https://<服务名>.onrender.com/healthz
# {"status":"ok","fake_streaming":false,"vertex":false,"keys":{"total":3,"active":3,"cooling":0,"disabled":0}}

curl -s -H "Authorization: Bearer <PASSWORD>" https://<服务名>.onrender.com/v1/models | grep -o 'gemini-3.8-flash' | head -1
```

### 4. 免费层保活

免费实例 15 分钟无流量会休眠，冷启动 30–60 秒。用 [cron-job.org](https://cron-job.org) 或 UptimeRobot 每 5–10 分钟 GET 一次 `/healthz` 即可（该接口免鉴权、不消耗 Gemini 额度）。

## 三、接入 DSH

设置 → 模型（Models）→ 添加模型提供商 → **自定义模型 API**：

- Provider ID：`hajimi`
- 端点：`https://<服务名>.onrender.com/v1`
- 协议：OpenAI Chat Completions
- API 密钥：就是 `PASSWORD`
- 模型：`gemini-3.8-flash`

对应的配置文件长这样（`$DSH_HOME/profiles/<profile>/cordis.patch.yml`）：

```yaml
providers:
  hajimi:
    apiKeyEnv: HAJIMI_API_KEY
    api: openai-completions
    baseURL: https://<服务名>.onrender.com/v1
    models:
      gemini-3.8-flash: { name: gemini-3.8-flash }
```

密钥写在 `$DSH_HOME/.credentials.yaml` 的 `refs.HAJIMI_API_KEY`，只填裸值，不要带 `Bearer `。

接好后让 DSH 跑一个带工具的任务（比如读文件、搜网页），能正常返回 tool_calls 就说明补丁生效了。

## 四、密钥轮询怎么工作

每个请求从「健康 key 池」里挑一个：优先在途少、连续失败少、最久没被用过的 key，所以并发会被摊开，不会一直压着同一个 key 打。

失败按类型处理：

| 上游响应 | 处理 |
| --- | --- |
| 401 / 400 `API key not valid` | 该 key 永久下线（重启或手动 reset 才回来） |
| 429 配额超限（反复出现） | 冷却到太平洋时间零点（Google 免费额度按这个时间重置） |
| 429 分钟级限流 | 15s 起指数退避，最多 300s，遵守 `Retry-After` |
| 5xx / 连接失败 | 5–30s 短暂冷却 |
| 400 schema 类错误 | 不惩罚 key（是请求本身的问题） |

全部 key 都在冷却时会 fail-open（挑最早恢复的那个继续试），不会直接整站不可用。

排障命令（PASSWORD 换成你的）：

```bash
# 每个 key 的状态：in_flight / failures / cooldown_remaining / last_error_kind
curl -s -H "Authorization: Bearer <PASSWORD>" https://<服务名>.onrender.com/v1/keys

# 全部恢复
curl -s -X POST -H "Authorization: Bearer <PASSWORD>" -H "Content-Type: application/json"   -d '{}' https://<服务名>.onrender.com/v1/keys/reset

# 只恢复某一个（完整 key 或前缀都行）
curl -s -X POST -H "Authorization: Bearer <PASSWORD>" -H "Content-Type: application/json"   -d '{"key":"AIzaSyxxxxxx"}' https://<服务名>.onrender.com/v1/keys/reset
```

`/healthz` 里的 `keys` 字段可以直接用来做监控：`cooling`/`disabled` 长期不归零说明 key 池该补了。

## 五、排障

- **仍然报 `additionalProperties`**：说明跑的还是旧镜像。看 `GET /healthz` 是否 200，再确认 Render 服务用的仓库/分支是本目录。
- **`/v1/models` 为空**：key 无效或没配。启动日志里搜「加载可用模型」。
- **回答是「所有API密钥均请求失败」**：现在后面会跟一行「最近一次上游错误: …」，按那行去查（配额、key 失效、模型名不支持等）。
- **冷启动慢**：免费层正常现象，保活任务能缓解。
