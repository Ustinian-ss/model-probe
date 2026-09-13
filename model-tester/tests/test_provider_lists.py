"""离屏 GUI 测试：Provider 清单独立（不累计）+ 初始清单策略 + 手动重置/加载内置。

运行：model-tester 目录下
    .venv/Scripts/python.exe -m pytest tests/test_provider_lists.py -q

清单策略：
- NVIDIA NIM 官方端点 → 初始为内置 models.json 清单
- 其他 Provider → 初始为空清单（等「拉取模型」）
- 各 Provider 清单独立，切换不累计

说明：monkeypatch 掉 load_providers / save_providers，
不会读写用户真实的 ~/.nvidia_model_tester/providers.json。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import pytest
from PySide6.QtWidgets import QApplication

import model_store
import ui.main_window as MW
from providers import Provider

NVIDIA = Provider(name="NVIDIA", base_url="https://integrate.api.nvidia.com/v1")
BUILTIN_N = len(model_store.load_model_items())  # 内置清单数量（30）


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, monkeypatch):
    # 注意：Provider 的 base_url 默认值就是 NVIDIA 端点，
    # 第三方 Provider 必须显式传非 NVIDIA 的 base_url
    monkeypatch.setattr(
        MW, "load_providers",
        lambda: [
            Provider(name="ProvA", base_url="https://prova.example.com/v1"),
            NVIDIA,
            Provider(name="ProvB", base_url="https://provb.example.com/v1"),
        ],
    )
    w = MW.MainWindow()
    yield w
    if w.worker is not None and w.worker.isRunning():
        w.worker.stop()
    w.close()


def test_initial_list_empty_for_custom_provider(window):
    """启动时首个 provider 是第三方端点 → 清单为空，不预置内置。"""
    assert window.provider.name == "ProvA"
    assert window.items == []


def test_nvidia_endpoint_gets_builtin_catalog(window):
    """NVIDIA 官方端点 → 初始/首次进入均为内置清单。"""
    window.provider_combo.setCurrentIndex(1)  # 走真实信号切到 NVIDIA
    assert window.provider.name == "NVIDIA"
    assert len(window.items) == BUILTIN_N

    # 重置后仍回到内置清单
    window._merge_items(["extra/one"])
    window._reset_model_list()
    assert len(window.items) == BUILTIN_N


def test_switch_provider_does_not_accumulate(window):
    """切 Provider 后清单回到各自独立状态，而不是累计。"""
    # ProvA（当前，空）拉取 2 个
    assert window._merge_items(["provA/only-1", "provA/only-2"]) == 2
    assert len(window.items) == 2

    # 切到 ProvB：全新空清单，不带 ProvA 的累计
    window.provider_combo.setCurrentIndex(2)
    assert window.provider.name == "ProvB"
    assert window.items == []

    assert window._merge_items(["provB/only-1"]) == 1
    assert len(window.items) == 1

    # 切回 ProvA：恢复 ProvA 自己的清单（2 个），无 ProvB 项
    window.provider_combo.setCurrentIndex(0)
    ids = {it.id for it in window.items}
    assert len(window.items) == 2
    assert "provA/only-1" in ids and "provA/only-2" in ids
    assert "provB/only-1" not in ids


def test_switch_clears_test_status(window):
    """切 Provider 后旧测试状态不残留。"""
    window._merge_items(["provA/only-1"])
    target = next(it for it in window.items if it.id == "provA/only-1")
    target.status = "success"
    target.latency_ms = 123

    window.provider_combo.setCurrentIndex(2)
    window.provider_combo.setCurrentIndex(0)
    restored = next(it for it in window.items if it.id == "provA/only-1")
    assert restored.status == "pending"
    assert restored.latency_ms is None


def test_reset_list_clears_to_provider_default(window):
    """第三方 Provider「重置列表」→ 清空；NVIDIA 端点 → 回到内置清单。"""
    window._merge_items(["x/1", "x/2"])
    window._reset_model_list()
    assert window.items == []                      # 第三方：初始清单为空
    assert window.visible_items == []

    window.provider_combo.setCurrentIndex(1)       # NVIDIA
    window._merge_items(["y/1"])
    window._reset_model_list()
    assert len(window.items) == BUILTIN_N          # NVIDIA：初始清单为内置


def test_load_builtin_catalog_merges(window):
    """第三方 Provider 手动「加载内置清单」→ 内置 30 个并入（去重）。"""
    assert window._load_builtin_catalog() is None
    assert len(window.items) == BUILTIN_N

    # 再次加载：去重，无新增
    before = len(window.items)
    window._load_builtin_catalog()
    assert len(window.items) == before

    # 与拉取结果共存
    window._merge_items(["mio/custom-1"])
    assert len(window.items) == BUILTIN_N + 1


def test_start_test_blocks_on_empty_list(window, monkeypatch):
    """清单为空时开始测试给出明确指引，而不是“请至少选择一个模型”。"""
    calls = []
    monkeypatch.setattr(
        MW.QMessageBox, "warning", staticmethod(lambda *a, **k: calls.append(a[1]))
    )
    window.provider.api_key = "test-key"
    window._start_test()
    assert calls and "清单为空" in calls[-1]
    assert window.worker is None


def test_delete_provider_drops_cached_list(window, monkeypatch):
    """删除 Provider 后其缓存清单一并丢弃，不复活。"""
    monkeypatch.setattr(MW, "save_providers", lambda ps: None)
    monkeypatch.setattr(MW.QMessageBox, "information", staticmethod(lambda *a, **k: None))

    window._merge_items(["provA/only-9"])
    window.provider_combo.setCurrentIndex(2)  # 切到 ProvB
    window._delete_provider()                 # 删除当前 ProvB

    assert [p.name for p in window.providers] == ["ProvA", "NVIDIA"]
    assert "ProvB" not in window._provider_lists
    window.provider_combo.setCurrentIndex(0)  # 回 ProvA
    ids = {it.id for it in window.items}
    assert "provA/only-9" in ids              # ProvA 清单完好
