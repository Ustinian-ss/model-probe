# Nvidia Provider Model Connection Tester

批量测试 OpenAI-compatible Provider 模型连通性的 Windows 桌面工具。

## 功能

- 可配置多个 Provider，可切换当前 Provider
- Provider 内容包括 Name、Base URL、API Key、超时时间、并发数
- 内置 Nvidia NIM 常见模型列表
- 支持批量测试、停止测试、筛选模型
- 支持自动检测、流式模式、非流式模式
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
