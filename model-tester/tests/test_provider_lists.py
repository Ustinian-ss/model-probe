"""离屏 GUI 测试：Provider 切换时模型清单独立（不累计）+ 手动重置列表。

运行：model-tester 目录下
    .venv/Scripts/python.exe -m pytest tests/test_provider_lists.py -q

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

import ui.main_window as MW
from providers import Provider


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, monkeypatch):
    # 两个假 Provider，避免触碰真实配置文件
    monkeypatch.setattr(
        MW, "load_providers",
        lambda: [Provider(name="ProvA"), Provider(name="ProvB")],
    )
    w = MW.MainWindow()
    yield w
    w.close()


def test_initial_list_is_builtin(window):
    assert len(window.items) > 0
    assert len(window.items) == len(window.visible_items)


def test_switch_provider_does_not_accumulate(window):
    """切 Provider 后清单回到该 Provider 自己的（或内置）清单，而不是累计。"""
    n0 = len(window.items)

    # ProvA 拉取 2 个新模型
    assert window._merge_items(["provA/only-1", "provA/only-2"]) == 2
    assert len(window.items) == n0 + 2

    # 切到 ProvB：应为全新内置清单，不带 ProvA 的累计
    window._change_provider(1)
    assert window.provider.name == "ProvB"
    assert len(window.items) == n0
    assert "provA/only-1" not in {it.id for it in window.items}

    # ProvB 拉取 1 个
    assert window._merge_items(["provB/only-1"]) == 1
    assert len(window.items) == n0 + 1

    # 切回 ProvA：恢复 ProvA 自己的清单（含其拉取项，不含 ProvB 的）
    window._change_provider(0)
    ids = {it.id for it in window.items}
    assert len(window.items) == n0 + 2
    assert "provA/only-1" in ids and "provA/only-2" in ids
    assert "provB/only-1" not in ids


def test_switch_clears_test_status(window):
    """切 Provider 后旧测试状态不残留。"""
    n0 = len(window.items)
    window._merge_items(["provA/only-1"])
    target = next(it for it in window.items if it.id == "provA/only-1")
    target.status = "success"
    target.latency_ms = 123

    window._change_provider(1)
    window._change_provider(0)
    restored = next(it for it in window.items if it.id == "provA/only-1")
    assert restored.status == "pending"
    assert restored.latency_ms is None


def test_reset_list_restores_builtin(window):
    """手动「重置列表」一键还原内置清单。"""
    n0 = len(window.items)
    window._merge_items(["x/1", "x/2"])
    assert len(window.items) == n0 + 2

    window._reset_model_list()
    assert len(window.items) == n0
    assert all(not it.id.startswith("x/") for it in window.items)
    assert window.visible_items == window.items


def test_delete_provider_drops_cached_list(window, monkeypatch):
    """删除 Provider 后其缓存清单一并丢弃，不复活。"""
    monkeypatch.setattr(MW, "save_providers", lambda ps: None)
    monkeypatch.setattr(MW.QMessageBox, "information", staticmethod(lambda *a, **k: None))

    window._merge_items(["provA/only-9"])
    window.provider_combo.setCurrentIndex(1)  # 走真实信号路径切到 ProvB
    window._delete_provider()                 # 删除当前 ProvB

    assert [p.name for p in window.providers] == ["ProvA"]
    assert "ProvB" not in window._provider_lists
    ids = {it.id for it in window.items}
    assert "provA/only-9" in ids        # ProvA 清单完好
