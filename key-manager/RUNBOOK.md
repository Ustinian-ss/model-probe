# NVIDIA API Key 轮换运维 Runbook

> 项目：`F:\AI agent code\morekey`（原路径 `F:\morekey`，已合并至 AI agent code 目录）
> 适用对象：日常运维 / 应急响应 / 安全事件
> 当前 Key 数：**11**（每次轮换目标：先 +1 后 -1，新旧并存灰度）

---

## 0. 一句话总览

轮换 Key 的本质是**「让 N 个旧 Key 与 N+1 个新 Key 短暂共存，让客户端先认新 Key、旧 Key 自动退役」**。本项目的 `key_manager` 已经原生支持「新加即生效、旧 Key 失败自动进入冷却」，所以只需要保证 `.env` 里同时存在新旧 Key 即可。

---

## 1. 适用范围与触发条件

| 场景 | 严重度 | 是否立即轮换 |
|------|--------|--------------|
| 常规周期（建议 90 天） | 低 | 计划任务 |
| 员工离职 | 中 | 24h 内 |
| Key 在日志/截图/会话/邮件里**明文泄漏** | **高** | **立刻** |
| Git 仓库误提交 | **高** | **立刻** |
| NVIDIA 公告 Key 风险 | 高 | 24h 内 |
| 监控发现异常流量 | 高 | 立刻冻结 → 调查 → 轮换 |

---

## 2. 前置条件

1. **操作员**：拥有 [https://org.ngc.nvidia.com/setup/api-keys](https://org.ngc.nvidia.com/setup/api-keys) 的管理员/可生成 Key 权限的账号。
2. **本地环境**：本机有项目的写权限，且 `.env` 的 ACL 仅当前用户可读写（当前已配置为 `USTINIAN\28102:(R,W)`）。
3. **代码就绪**：`key_manager.py` 已是最新版（具备失败冷却 + 轮询切换能力）。
4. **无 Git 泄漏面**：当前项目**不是 git 仓库**（已确认），如果未来纳入版本控制，必须确保 `.env` 在 `.gitignore` 中（已配置）。

---

## 3. 轮换流程（标准 6 步）

### Step 1 ── 登录 NGC 生成新 Key

访问 [https://org.ngc.nvidia.com/setup/api-keys](https://org.ngc.nvidia.com/setup/api-keys) → **Generate Personal Key**：

- **Name**：建议带上日期与用途，例如 `morekey-prod-2025-09-01`。
- **Expiration**：建议 90 天（NVIDIA 强制最长 365 天）。
- **Services Included**：选择 **NIM**（其它按需）。

生成后页面**只显示完整 Key 一次**，立刻复制并保存到密码管理器（1Password / Bitwarden / KeePass）。

> ⚠️ **不要**把新 Key 直接贴在聊天里、邮件里、issue 里、或任何非加密介质。

### Step 2 ── 验证新 Key 单独可用

在写进 `.env` 之前先用 `curl` 跑一发小请求，确认 Key 有效：

```bash
NEW_KEY="nvapi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

curl -sS -X POST \
  "https://integrate.api.nvidia.com/v1/chat/completions" \
  -H "Authorization: Bearer $NEW_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-oss-120b",
    "messages": [{"role":"user","content":"ping"}],
    "max_tokens": 8
  }'
```

期望：返回 `choices[0].message.content`，HTTP 200。如果返回 401 → Key 无效或未开启 NIM 权限。

### Step 3 ── 把新 Key 追加进 `.env`（**追加，不替换**）

打开 `F:\AI agent code\morekey\.env`（原路径 `F:\morekey\.env`），在 `NVIDIA_API_KEYS=` 行末**追加**新 Key：

```
NVIDIA_API_KEYS=nvapi-旧的1,...,nvapi-旧的N,nvapi-新的
```

这一时刻，**新旧 Key 同时生效**。`KeyManager` 会把新 Key 也纳入轮询池，自动开始承担流量。

推荐用项目里提供的辅助脚本（见第 6 节）做这一步，避免手抖：

```bash
python rotate_keys.py add --new "$NEW_KEY"
```

### Step 4 ── 观察灰度（≥ 24 小时）

观察以下信号，确认新 Key 工作正常：

| 信号 | 检查方式 | 期望 |
|------|----------|------|
| `KeyManager.stats()` 失败计数 | 业务日志 | 新 Key 计数 ≈ 0 |
| 业务侧错误率 | 监控 / 日志 | 与轮换前持平，无尖峰 |
| `KeyManager.report_failure` | 日志 | 没有针对新 Key 的 401/429 |
| NVIDIA NGC 使用量 | NGC 控制台 → Usage | 新 Key 开始有调用 |

```python
# 业务代码里任意位置打印
from key_manager import KeyManager
print(km.stats())   # {失败 Key: 失败次数}
```

### Step 5 ── 从 `.env` 移除旧 Key

灰度通过后，把要退役的 Key 从 `NVIDIA_API_KEYS=` 行中删除：

```bash
python rotate_keys.py remove --old "nvapi-旧 Key 完整值"
```

> ⚠️ 删之前再 cat 一次 `.env`，**人眼核对**要删的 Key 是不是就那一个，避免一次删错。

### Step 6 ── 在 NGC 撤销旧 Key

回到 [https://org.ngc.nvidia.com/setup/api-keys](https://org.ngc.nvidia.com/setup/api-keys)，找到对应 Name 的 Key → **Delete**。

**这一步不可逆**，所以务必：
- 已完成 Step 5（从 `.env` 删除）
- 已完成 Step 4（≥ 24h 灰度观察）

---

## 4. 紧急轮换（Key 泄漏场景）

跳过灰度，**直接全量替换**：

1. **立刻**在 NGC 撤销被泄漏的 Key（不让它继续可调用）。
2. 同时生成新的 Key（数量不少于被撤销的）。
3. 直接覆盖 `.env` 的 `NVIDIA_API_KEYS=` 行（不是追加，是**整行替换**）。
4. 重启依赖 `.env` 的进程 / 服务，让新 Key 加载。
5. 在事后复盘里登记事件时间、泄漏渠道、根因、修复动作。

```bash
# 一键替换（强烈建议 Step 1 之前先把 .env 备份到密码管理器/保险库）
cp .env .env.bak.$(date +%Y%m%d-%H%M%S)
# 编辑 .env，把 NVIDIA_API_KEYS= 整行改成新的 Key 列表
# 重启进程
```

---

## 5. 监控与告警建议

| 指标 | 阈值 | 建议动作 |
|------|------|----------|
| 单 Key 5xx 比率 | > 5% | 触发 `report_failure`，自动冷却 60s |
| 单 Key 401 比率 | > 0% | 立即停止使用并轮换 |
| Key 总量 | < 2 | 告警：单点故障风险 |
| 全部 Key 同时失败 | 任何 | 告警：服务降级 |
| Key 剩余有效期 | < 14 天 | 触发计划轮换任务 |

`KeyManager` 内置的 `cooldown_seconds=60.0` 是合理默认值；如果上游限流更激进，可调到 30s。

---

## 6. 辅助脚本 `rotate_keys.py`

把它放到项目根目录（与 `key_manager.py` 同级），提供 `add` / `remove` / `list` / `rotate` 子命令，避免人工编辑 `.env` 出错。

```python
"""NVIDIA API Key 轮换辅助脚本。

子命令：
    list                       列出当前所有 Key
    add   --new KEY            追加一个新 Key
    remove --old KEY           删除一个旧 Key
    rotate --old KEY --new KEY 一步完成：追加新 Key → 灰度 → 删除旧 Key（不自动删 NGC 端）
    audit                      把 Key 的 SHA256 前 8 位打印出来，便于和 NGC 端对照

不会触碰 .env 之外的任何文件。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"
KEY_LINE_PREFIX = "NVIDIA_API_KEYS="


def load_keys() -> list[str]:
    if not ENV_FILE.exists():
        raise SystemExit(f"找不到 {ENV_FILE}")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(KEY_LINE_PREFIX):
            raw = line[len(KEY_LINE_PREFIX):]
            return [k.strip() for k in raw.split(",") if k.strip()]
    raise SystemExit(f"{ENV_FILE} 中没有 {KEY_LINE_PREFIX} 行")


def save_keys(keys: list[str]) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    new_line = KEY_LINE_PREFIX + ",".join(keys)
    new_lines = [new_line if l.startswith(KEY_LINE_PREFIX) else l for l in lines]
    if not any(l.startswith(KEY_LINE_PREFIX) for l in lines):
        new_lines.insert(0, new_line)
    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def fingerprint(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()[:8]


def cmd_list(_: argparse.Namespace) -> None:
    keys = load_keys()
    print(f"共 {len(keys)} 个 Key：")
    for i, k in enumerate(keys, 1):
        print(f"  {i:2d}. {fingerprint(k)}  {k[:12]}…{k[-6:]}")


def cmd_add(args: argparse.Namespace) -> None:
    keys = load_keys()
    new = args.new.strip()
    if not new.startswith("nvapi-"):
        raise SystemExit("新 Key 格式异常（应以 nvapi- 开头）")
    if new in keys:
        print("该 Key 已在 .env 中，无需重复添加。")
        return
    keys.append(new)
    save_keys(keys)
    print(f"已追加新 Key（{fingerprint(new)}），共 {len(keys)} 个 Key。")


def cmd_remove(args: argparse.Namespace) -> None:
    keys = load_keys()
    old = args.old.strip()
    if old not in keys:
        print(f"未在 .env 中找到该 Key（{fingerprint(old)}）。请核对完整值。")
        sys.exit(2)
    keys = [k for k in keys if k != old]
    save_keys(keys)
    print(f"已移除 Key（{fingerprint(old)}），剩余 {len(keys)} 个。")


def cmd_rotate(args: argparse.Namespace) -> None:
    """追加新 Key 后立即删除旧 Key。**不**做灰度；紧急场景下使用。"""
    cmd_add(argparse.Namespace(new=args.new))
    cmd_remove(argparse.Namespace(old=args.old))


def cmd_audit(_: argparse.Namespace) -> None:
    keys = load_keys()
    print("Key 指纹（用于与 NGC 控制台对照，不会泄露完整 Key）：")
    for k in keys:
        print(f"  {fingerprint(k)}  ({k[:10]}…)")


def main() -> None:
    p = argparse.ArgumentParser(description="NVIDIA API Key 轮换助手")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出所有 Key").set_defaults(func=cmd_list)
    a = sub.add_parser("add", help="追加一个新 Key")
    a.add_argument("--new", required=True)
    a.set_defaults(func=cmd_add)
    r = sub.add_parser("remove", help="删除一个旧 Key")
    r.add_argument("--old", required=True)
    r.set_defaults(func=cmd_remove)
    rot = sub.add_parser("rotate", help="一步替换（紧急场景，不做灰度）")
    rot.add_argument("--old", required=True)
    rot.add_argument("--new", required=True)
    rot.set_defaults(func=cmd_rotate)
    sub.add_parser("audit", help="打印 Key 指纹").set_defaults(func=cmd_audit)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
```

### 用法示例

```bash
# 查看当前 Key 数与指纹
python rotate_keys.py list

# 灰度轮换（推荐流程）
python rotate_keys.py add    --new "nvapi-新Key"
# ... 观察 24h ...
python rotate_keys.py remove --old "nvapi-旧Key"

# 紧急替换（泄漏场景，跳过灰度）
python rotate_keys.py rotate --old "nvapi-旧Key" --new "nvapi-新Key"

# 只看指纹，避免把完整 Key 贴进聊天
python rotate_keys.py audit
```

---

## 7. 验证

写完 runbook 之后，对本项目做一次自检：

| 项 | 命令 / 检查 | 期望 |
|----|-------------|------|
| `rotate_keys.py` 能列出当前 11 个 Key | `python rotate_keys.py list` | 输出 11 行 |
| `add` 子命令可追加 | `python rotate_keys.py add --new nvapi-test` | 列表变 12 |
| `remove` 子命令可删除 | `python rotate_keys.py remove --old nvapi-test` | 列表回 11 |
| 完整 Key 不出现在 audit 输出 | `python rotate_keys.py audit` | 只显示前 10 字符 + 指纹 |
| 业务代码不需改动 | `python -c "from nvidia_client import NvidiaClient; print(len(NvidiaClient().manager.keys))"` | 11 |

---

## 8. 回滚

如果轮换后业务出错，回滚方式：

1. **如果还在灰度（Step 4）**：从 `.env` 删除新 Key → 重启进程 → 旧 Key 自动顶回。
2. **如果已经移除旧 Key（Step 5 之后）**：在 NGC 重新生成同一个 Name 的 Key（Key 值不同，但 NGC 端可以识别 Name）→ 用 `rotate_keys.py add` 加回 `.env`。
3. **如果 NGC 端 Key 已删（Step 6 之后）**：必须重新生成一个**全新** Key（旧的不可恢复），走完整 6 步流程。

---

## 9. 后续改进（待办）

- [ ] 把 `.env` 替换为加密的 `secrets.toml` + OS Keyring
- [ ] 接 AWS Secrets Manager / HashiCorp Vault
- [ ] 加 `pre-commit` + `detect-secrets` 防误提交
- [ ] 接入 Prometheus 指标：`key_active`, `key_cooldown`, `key_failure_total`
- [ ] 把 `rotate_keys.py` 纳入 CI（lint + dry-run）