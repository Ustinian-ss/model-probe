# 多个 NVIDIA API Key 管理示例

统一管理多个 NVIDIA API Key：轮询 / 随机 / 加权 / **余量优先**调度，按状态分级冷却，失败自动换 Key 重试。
内置滑动窗口限流（默认每把 Key 40 次/分钟，对应 NVIDIA 免费层）：请求发出前就避开已经打满的 Key，
而不是撞上 429 再补救。

## 快速开始

1. 安装依赖

   ```bash
   pip install -r requirements.txt
   ```

2. 配置 Key

   ```bash
   cp .env.example .env
   # 编辑 .env，把 nvapi-xxx 换成真实 Key，逗号分隔
   ```

3. 使用

   ```python
   from nvidia_client import NvidiaClient

   client = NvidiaClient()
   reply = client.chat([{"role": "user", "content": "你好"}])
   print(reply)
   ```

## 调度策略

```python
from nvidia_client import NvidiaClient

# 轮询（默认）
client = NvidiaClient(strategy="round_robin")

# 随机
client = NvidiaClient(strategy="random")

# 加权：key 和权重
client = NvidiaClient(
    keys=[("nvapi-high", 5), ("nvapi-low", 1)],
    strategy="weighted",
)

# 余量优先（多 Key 池推荐）：窗口内剩余次数最多者优先，
# 并列时挑延迟 EMA 最小的那把 —— 也就是“哪个快用哪个”
client = NvidiaClient(strategy="least_loaded")   # 别名 "smart"
```

## 限流与冷却

```python
from key_manager import KeyManager, KeyUnavailableError

km = KeyManager(
    keys=["nvapi-a", "nvapi-b"],
    strategy="least_loaded",
    rpm_per_key=40,               # 每把 Key 每窗口上限；None = 不限流
    rpm_per_account=None,         # 可选：按账号再限一层（配合 accounts={key: 账号}）
    window_seconds=60.0,
    cooldown_429_seconds=20.0,    # 429：优先用上游 Retry-After，否则 20s
    cooldown_5xx_seconds=2.0,     # 5xx：短冷却，避免整个池被一次瞬时故障拖停
    auth_cooldown_seconds=300.0,  # 401/403：长冷却，但不永久拉黑
)

key = km.next()                              # 全不可用 → 抛 KeyUnavailableError（带 retry_after）
km.report_success(key, latency_ms=elapsed)   # 清冷却 + 更新延迟 EMA
km.report_failure(key, status=429, retry_after=12)
print(km.stats())                            # 每把 Key：余量 / 失败数 / 最近状态 / 延迟 EMA
```

要点：

- `next()` 不会静默返回一把已经超额的 Key：全不可用时抛 `KeyUnavailableError`，`retry_after` 告诉你等多久；
- 同一请求内重试传 `exclude=[用过的 Key]`，模型级故障转移传 `relax=True`（放过冷却，但**不放宽限流**）；
- 从旧版本升级：`strategy` 默认值仍是 `round_robin`，新增参数都是可选关键字参数；唯一的行为变化是
  **默认开始限流 40/分钟/Key**，不需要就传 `rpm_per_key=None`。

## 客户端

- `client.chat(...)`：一次调用内部自动换 Key 重试（429/401/5xx 都算失败），`max_attempts` 可调；
- `client.chat_stream(...)`：逐段吐出文本，同时读取 `delta.content` 与 `delta.reasoning_content`
  （推理模型的正文常常只在后者里，只读 content 会误报“空响应”），能识别 `data: {"error": ...}`
  ——包括**上游用 HTTP 200 裹错误体** 这种怪癖，也容忍不发 `[DONE]` 的上游；
- 用 `with NvidiaClient(...) as c:` 或记得 `c.close()`：httpx 连接池会按 Key 复用连接。

## 安全提醒

- 不要把 `.env` 提交到 Git（已加入 `.gitignore`）。
- 不要在日志里打印完整 Key。
- 生产环境建议用密钥管理服务（AWS Secrets Manager / Vault 等）。
