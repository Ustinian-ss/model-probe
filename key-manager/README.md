# 多个 NVIDIA API Key 管理示例

统一管理多个 NVIDIA API Key：轮询 / 随机 / 加权调度，失败自动切换（429/401 换 Key 重试）。

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
```

## 安全提醒

- 不要把 `.env` 提交到 Git（已加入 `.gitignore`）。
- 不要在日志里打印完整 Key。
- 生产环境建议用密钥管理服务（AWS Secrets Manager / Vault 等）。
