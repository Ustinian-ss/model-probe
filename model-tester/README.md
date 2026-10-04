# Nvidia Provider Model Connection Tester

批量测试 OpenAI-compatible Provider 模型连通性的 Windows 桌面工具。

## 功能

- 可配置多个 Provider，可切换当前 Provider
- **每个 Provider 独立维护模型清单**：切换 Provider 自动载入各自的清单（含各自的拉取结果），互不累计
- **初始清单策略**：NVIDIA NIM 官方端点预置内置 30 个模型（开箱即测）；其他 Provider 首次进入为**空清单**，点「拉取模型」获取该端点的真实模型列表
- **「重置列表」按钮**（Ctrl+Shift+L）：一键清空当前 Provider 的清单（含拉取结果），恢复初始清单（NVIDIA 端点=内置清单，其他=空）
- **「加载内置清单」按钮**（Ctrl+Shift+B）：把内置的 NVIDIA NIM 模型并入当前 Provider 清单（去重，不覆盖已有项）
- Provider 内容包括 Name、Base URL、API Key、超时时间、并发数
- 内置 Nvidia NIM 常见模型列表
- 支持批量测试、停止测试、筛选模型
- **导出可用模型**（状态为成功的模型）到 CSV，另提供「导出全部」按钮保留完整记录（含失败/未测试）
- 显示状态、延迟、是否支持流式、错误信息和响应片段

## 运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe src\main.py
```

## 打包

```powershell
.\build_exe.ps1
```

产物为 `dist\NvidiaModelTester.exe`（onefile + windowed，已通过 `--add-data "config;config"` 内置 `config/models.json` 模型清单）。

## 安全提示

- Provider 配置（含 **API Key 明文**）保存在 `~/.nvidia_model_tester/providers.json`。请勿在多用户机器或会被同步/备份到不受控位置的家目录上使用；必要时删除该文件即可清除 Key。
- 保存时会自动留一份 `providers.json.bak`；解析失败时会备份为 `providers.json.corrupt`。

## 流式响应健壮性

流式解析（`src/tester.py` 的 `parse_sse_chunk_lines`，纯函数、可单测）处理了三个真实踩过的坑：

- **只读 `delta.content` 会误判空响应**：推理模型（nemotron / gpt-oss 系列）的正文常常只在
  `delta.reasoning_content` 里，现在两者都读；
- **上游用 HTTP 200 裹错误体**（`Service temporarily overloaded` / `Internal server error`）：
  以前会被当成成功并显示空内容，现在识别成失败并给出原因；
- **有的上游不发 `data: [DONE]`**：以 `[DONE]` 或 `finish_reason` 任一出现作为正常收尾，不会挂死或误报。

## 打包成 exe（重要：改了源码就必须重打包）

exe **不进版本库**（`.gitignore` 里有 `dist/` `build/` `*.exe`），所以仓库里只有源码；
本机双击运行的 `NvidiaModelTester.exe` 必须由源码重新打包，否则跑的仍是旧代码。

### 最省事：双击 `启动模型测试器.cmd`

它每次先判断「`src/`、`config/`、`requirements.txt` 有没有比 exe 新」：

- 有更新 → 自动重新打包（首次或大改约 1–3 分钟；之后命中增量缓存通常几秒），再启动；
- 已是最新 → 直接启动，不浪费时间。

### 手动打包

```powershell
cd model-tester
powershell -ExecutionPolicy Bypass -File build_exe.ps1              # 含 pip install（需要联网时）
powershell -ExecutionPolicy Bypass -File build_exe.ps1 -SkipDeps    # 依赖没变，离线快速重打包
```

`build_exe.ps1` 用 `--onefile --windowed --add-data "config;config"` 打包，并把产物
**自动覆盖**顶层的 `NvidiaModelTester.exe`（以前要手动复制，忘了就会双击到旧代码，且复制失败还不报错）。
`config/models.json`（内置模型清单）必须打进包，否则 exe 里清单是空的。

### 只检查、不打包

```powershell
powershell -ExecutionPolicy Bypass -File rebuild-if-stale.ps1 -Check
# 退出码：0 = 已是最新；10 = 需要重新打包
```

判断依据只算**真正打进包里的东西**（`src/`、`config/`、`requirements.txt`），并且忽略
`__pycache__`/`.pyc` —— 否则跑一次测试就会把 exe 判成“旧”，每次双击都白等两分钟。

### 提交前提醒（git hook）

仓库带了一个 pre-commit 钩子：改了 `model-tester/src|config` 却没重打包时会提醒你
（默认只提醒、不拦提交；要强制拦截：`MP_STRICT_EXE=1 git commit ...`）。启用一次即可：

```bash
git config core.hooksPath .githooks
```

> 两个 `.ps1` 含中文，**必须保存为 UTF-8 with BOM**：PowerShell 5.1 在没有 BOM 时按 GBK 解析，
> 中文全角括号会把后面的引号“吃掉”，直接报语法错误。
