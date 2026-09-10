# model-probe

批量测试 OpenAI 兼容 Provider **模型连通性**的工具集。

## 子项目

| 目录 | 用途 |
|---|---|
| `model-tester/` | **核心**：Windows GUI（PySide6）批量测试各模型可用性/延迟/流式支持，支持多 Provider、并发、CSV 导出、PyInstaller 打包 |
| `key-manager/` | 多 NVIDIA API Key 调度：轮询/加权/失败冷却/自动切换，含 `.env` 轮换脚本与运维 RUNBOOK |
| `data-scripts/` | 针对本地历史数据快照的一次性分析脚本（聚合统计） |

## 快速开始（model-tester）

```powershell
cd model-tester
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe src\main.py
```

打包为单文件 exe：`.\build_exe.ps1`（产物 `dist\NvidiaModelTester.exe`，内置模型清单）。

## 快速开始（key-manager）

```bash
cd key-manager
python rotate_keys.py list    # 列出 Key（默认仅 SHA256 指纹）
python rotate_keys.py audit   # 指纹对照 NGC 控制台
python -m pytest tests/ -q    # 单元测试
```

> 原名 `nvidia-tools`，2026-09 更名为 `model-probe`（更贴合"批量探测连通性"的定位）。
