from __future__ import annotations

import concurrent.futures
import csv

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

import model_store
from models import ModelItem
from providers import (
    Provider,
    ensure_default_provider,
    load_providers,
    save_providers,
)
from tester import ProviderTester, TestConfig
from ui.models_table import ModelsTable
from ui.provider_dialog import ProviderDialog


class TestSignals(QObject):
    # 传递 ModelItem 引用本身（test_model 原地修改并返回同一对象），
    # 避免用“选中子集下标”回写 self.items 造成错位（修复部分勾选时结果写错行的 bug）
    row_updated = Signal(object)
    finished = Signal()
    progress = Signal(int, int, int, int)


class FetchSignals(QObject):
    succeeded = Signal(list)
    failed = Signal(str)


class TestWorker(QThread):
    def __init__(
        self,
        provider: Provider,
        config: TestConfig,
        items: list[ModelItem],
        parent=None,
    ):
        super().__init__(parent)
        self.provider = provider
        self.config = config
        self.items = items
        self.signals = TestSignals()
        self.tester = ProviderTester(provider, config)

    def run(self) -> None:
        selected = [item for item in self.items if item.selected]
        total = len(selected)
        success = 0
        failed = 0
        done = 0

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.provider.max_workers
        ) as executor:
            future_map = {
                executor.submit(self._run_one, item): item
                for item in selected
            }

            for future in concurrent.futures.as_completed(future_map):
                if self.tester.cancelled:
                    break

                item = future_map[future]
                result = future.result()
                done += 1
                if result.status == "success":
                    success += 1
                elif result.status == "failed":
                    failed += 1

                self.signals.row_updated.emit(result)
                self.signals.progress.emit(done, total, success, failed)

        self.tester.close()
        self.signals.finished.emit()

    def _run_one(self, item: ModelItem) -> ModelItem:
        # test_model 原地修改 item 并返回同一对象，self.items 无需按下标回写
        return self.tester.test_model(item)

    def stop(self) -> None:
        self.tester.cancel()
        self.wait(2000)


class FetchWorker(QThread):
    """在后台调 /v1/models 自动发现 provider 的模型清单。

    成功 emit succeeded(list[str])；失败 emit failed(str)。
    用独立 QThread 而非 worker pool，避免与批量测试并发冲突。
    """

    def __init__(self, provider: Provider, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.signals = FetchSignals()

    def run(self) -> None:
        tester = None
        try:
            tester = ProviderTester(self.provider, TestConfig())
            ids = tester.fetch_models()
            self.signals.succeeded.emit(ids)
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        finally:
            if tester is not None:
                tester.close()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.resize(1050, 660)

        self.providers = ensure_default_provider(load_providers())
        self.provider = self.providers[0]
        self.setWindowTitle(f"测试模型连接: {self.provider.name}")

        self.items = self._initial_items_for(self.provider)
        if (
            not self.items
            and self._is_builtin_catalog_provider(self.provider)
            and model_store.LAST_LOAD_ERROR
        ):
            QMessageBox.warning(
                self, "内置模型清单缺失",
                model_store.LAST_LOAD_ERROR + "\n\n可点击「拉取模型」从 Provider 获取清单。",
            )
        self.visible_items: list[ModelItem] = list(self.items)
        self.worker: TestWorker | None = None
        self.fetch_worker: QThread | None = None
        # 每个 Provider 独立维护一份模型清单（切换时互不累计）：
        # 切走时保存当前清单，切入时恢复该 Provider 自己的清单；首次使用回退内置清单
        self._provider_lists: dict[str, list[ModelItem]] = {}

        self.provider_combo = QComboBox()
        self.provider_combo.addItems([p.name for p in self.providers])
        self.provider_combo.currentIndexChanged.connect(self._change_provider)

        self.provider_button = QPushButton("配置 Provider")
        self.provider_button.clicked.connect(self._edit_provider)

        self.add_provider_button = QPushButton("新增 Provider")
        self.add_provider_button.clicked.connect(self._add_provider)

        self.delete_provider_button = QPushButton("删除 Provider")
        self.delete_provider_button.clicked.connect(self._delete_provider)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["自动检测", "流式模式", "非流式模式"])
        self.mode_combo.currentIndexChanged.connect(self._sync_preferred_mode)

        # 首选模式：自动检测时决定第一步用哪种；锁定模式时被禁用
        self.preferred_stream = QRadioButton("流式")
        self.preferred_nonstream = QRadioButton("非流式")
        self.preferred_stream.setChecked(True)   # 默认流式
        self._preferred_group = QButtonGroup(self)
        self._preferred_group.addButton(self.preferred_stream)
        self._preferred_group.addButton(self.preferred_nonstream)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("筛选模型...")
        self.search_edit.textChanged.connect(self._filter_items)

        self.start_button = QPushButton("开始测试")
        self.start_button.clicked.connect(self._start_test)

        self.stop_button = QPushButton("停止测试")
        self.stop_button.clicked.connect(self._stop_test)
        self.stop_button.setEnabled(False)

        self.fetch_button = QPushButton("拉取模型")
        self.fetch_button.clicked.connect(self._start_fetch)

        self.reset_list_button = QPushButton("重置列表")
        self.reset_list_button.setToolTip(
            "手动清空当前 Provider 的模型清单（含拉取结果），恢复初始清单："
            "NVIDIA 官方端点为内置清单，其他 Provider 为空 (Ctrl+Shift+L)"
        )
        self.reset_list_button.clicked.connect(self._reset_model_list)

        self.builtin_list_button = QPushButton("加载内置清单")
        self.builtin_list_button.setToolTip(
            "把内置的 NVIDIA NIM 模型清单（config/models.json）并入当前 Provider 的清单（自动去重）"
            " (Ctrl+Shift+B)"
        )
        self.builtin_list_button.clicked.connect(self._load_builtin_catalog)

        self.copy_button = QPushButton("复制 ID")
        self.copy_button.setToolTip("复制所有成功的模型 ID 到剪贴板 (Ctrl+Shift+C)")
        self.copy_button.clicked.connect(self._copy_success_ids)

        self.export_button = QPushButton("导出 CSV")
        self.export_button.setToolTip("导出全部测试结果到 CSV (Ctrl+Shift+E)")
        self.export_button.clicked.connect(self._export_csv)

        self.clear_results_button = QPushButton("清空结果")
        self.clear_results_button.setToolTip("清空所有测试状态 (Delete)")
        self.clear_results_button.clicked.connect(self._clear_results)

        self.progress_label = QLabel("待测试")
        self.progress_label.setStyleSheet("font-weight: bold;")

        self.table = ModelsTable()
        self.table.set_items(self.items)

        self._build_layout()
        self._setup_shortcuts()

    def _build_layout(self) -> None:
        top = QHBoxLayout()
        top.addWidget(QLabel("Provider"))
        top.addWidget(self.provider_combo)
        top.addWidget(self.provider_button)
        top.addWidget(self.add_provider_button)
        top.addWidget(self.delete_provider_button)
        top.addWidget(QLabel("端点模式"))
        top.addWidget(self.mode_combo)

        controls = QHBoxLayout()
        controls.addWidget(self.search_edit)
        controls.addWidget(QLabel("首选模式"))
        controls.addWidget(self.preferred_stream)
        controls.addWidget(self.preferred_nonstream)
        controls.addWidget(self.fetch_button)
        controls.addWidget(self.reset_list_button)
        controls.addWidget(self.builtin_list_button)
        controls.addWidget(self.copy_button)
        controls.addWidget(self.export_button)
        controls.addWidget(self.clear_results_button)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)

        layout = QVBoxLayout()
        layout.addLayout(top)
        layout.addLayout(controls)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.table)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    # ---------- 内置清单策略 ----------
    _NVIDIA_CATALOG_HOST = "integrate.api.nvidia.com"

    def _is_builtin_catalog_provider(self, provider: Provider) -> bool:
        """只有 NVIDIA NIM 官方端点才预置内置模型清单（对第三方端点该清单基本无效）。"""
        return self._NVIDIA_CATALOG_HOST in (provider.base_url or "")

    def _initial_items_for(self, provider: Provider) -> list[ModelItem]:
        """Provider 首次进入时的初始清单：
        - NVIDIA NIM 官方端点：内置 models.json 清单（开箱即测）
        - 其他 Provider：空清单，等「拉取模型」获取真实列表
        """
        if self._is_builtin_catalog_provider(provider):
            return model_store.load_model_items()
        return []

    def _change_provider(self, index: int) -> None:
        if 0 <= index < len(self.providers):
            # 如果从同一个 provider 切换到自己（重复点击），不清理
            if self.provider is self.providers[index]:
                return
            # 保存当前 Provider 的清单（各 Provider 独立，互不累计）
            self._provider_lists[self.provider.name] = self.items
            self.provider = self.providers[index]
            self.setWindowTitle(f"测试模型连接: {self.provider.name}")
            # 载入目标 Provider 自己的清单；首次使用该 Provider 则回退内置清单
            stored = self._provider_lists.get(self.provider.name)
            self.items = (
                list(stored) if stored is not None else self._initial_items_for(self.provider)
            )
            self.visible_items = list(self.items)
            self.table.set_items(self.visible_items)
            # 健壮性：切换 provider 后清空旧测试状态，避免误以为属于新 provider
            self._clear_results(silent=True)
            if self.items:
                self.progress_label.setText(
                    f"已切换到 {self.provider.name}，载入 {len(self.items)} 个模型，测试状态已清空"
                )
            else:
                self.progress_label.setText(
                    f"已切换到 {self.provider.name}，清单为空——请「拉取模型」获取真实清单，或「加载内置清单」"
                )

    def _add_provider(self) -> None:
        dialog = ProviderDialog(Provider(name=f"Provider {len(self.providers) + 1}"), self)
        if dialog.exec():
            self.providers.append(dialog.result_provider())
            save_providers(self.providers)
            self._refresh_provider_combo(-1)

    def _edit_provider(self) -> None:
        dialog = ProviderDialog(self.provider, self)
        if dialog.exec():
            new_provider = dialog.result_provider()
            old_name = self.provider.name
            self.providers[self.provider_combo.currentIndex()] = new_provider
            save_providers(self.providers)
            self.provider = new_provider
            # 改名场景：把清单缓存迁移到新名字下（清单本身不变）
            if new_provider.name != old_name:
                self._provider_lists.pop(old_name, None)
                self._provider_lists[new_provider.name] = self.items
            self.provider_combo.setItemText(self.provider_combo.currentIndex(), new_provider.name)
            self.setWindowTitle(f"测试模型连接: {new_provider.name}")
            QMessageBox.information(self, "已保存", "Provider 配置已保存")

    def _delete_provider(self) -> None:
        if len(self.providers) <= 1:
            QMessageBox.warning(self, "不能删除", "至少保留一个 Provider")
            return

        index = self.provider_combo.currentIndex()
        name = self.providers[index].name
        self.providers.pop(index)
        save_providers(self.providers)
        self._refresh_provider_combo(0)
        # 刷新会触发 _change_provider 把被删 Provider 的清单写回缓存，这里清掉
        self._provider_lists.pop(name, None)
        QMessageBox.information(self, "已删除", f"已删除 Provider: {name}")

    def _refresh_provider_combo(self, index: int) -> None:
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        self.provider_combo.addItems([p.name for p in self.providers])
        self.provider_combo.blockSignals(False)
        if index < 0:
            index = self.provider_combo.count() - 1
        self.provider_combo.setCurrentIndex(index)
        self._change_provider(index)

    def _filter_items(self, text: str) -> None:
        needle = text.strip().lower()
        self.visible_items = [item for item in self.items if needle in item.id.lower()]
        self.table.set_items(self.visible_items)

    def _start_test(self) -> None:
        if not self.provider.api_key:
            QMessageBox.warning(self, "缺少 API Key", "请先配置 Provider API Key")
            return

        if not self.items:
            QMessageBox.warning(
                self, "清单为空",
                "当前 Provider 的模型清单为空，请先「拉取模型」或「加载内置清单」。",
            )
            return

        self._apply_table_selection()
        selected_count = sum(item.selected for item in self.items)
        if selected_count == 0:
            QMessageBox.warning(self, "未选择模型", "请至少选择一个模型")
            return

        config = TestConfig(
            endpoint_mode=self._endpoint_mode(),
            prefer_stream=self.preferred_stream.isChecked(),
        )

        self.worker = TestWorker(self.provider, config, self.items)
        self.worker.signals.row_updated.connect(self._on_row_updated)
        self.worker.signals.progress.connect(self._on_progress)
        self.worker.signals.finished.connect(self._on_finished)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.worker.start()

    def _stop_test(self) -> None:
        if self.worker:
            self.worker.stop()

    def _on_row_updated(self, item: ModelItem) -> None:
        self.table.update_item(item)

    def _on_progress(self, done: int, total: int, success: int, failed: int) -> None:
        self.progress_label.setText(
            f"正在批量测试AI模型... 已完成 {done}/{total} · {success}个成功，{failed}个失败"
        )

    def _on_finished(self) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def _endpoint_mode(self) -> str:
        text = self.mode_combo.currentText()
        if text == "流式模式":
            return "stream"
        if text == "非流式模式":
            return "non_stream"
        return "auto"

    def _sync_preferred_mode(self, index: int) -> None:
        """端点模式切换时同步首选模式的状态：

        - 自动检测 (index=0): 启用，流式/非流式都可点
        - 流式模式 (index=1): 禁用，自动勾选流式
        - 非流式模式 (index=2): 禁用，自动勾选非流式
        """
        if index == 0:
            self.preferred_stream.setEnabled(True)
            self.preferred_nonstream.setEnabled(True)
        elif index == 1:
            self.preferred_stream.setEnabled(False)
            self.preferred_nonstream.setEnabled(False)
            self.preferred_stream.setChecked(True)
        elif index == 2:
            self.preferred_stream.setEnabled(False)
            self.preferred_nonstream.setEnabled(False)
            self.preferred_nonstream.setChecked(True)

    def _apply_table_selection(self) -> None:
        selected_ids = set(self.table.selected_ids())
        for item in self.items:
            # 必须双向同步：筛选时 selected_ids() 只含可见行，
            # 只置 True 不置 False 会让被筛掉的行残留旧勾选状态
            item.selected = item.id in selected_ids
            item.status = "pending"
            item.latency_ms = None
            item.streaming_supported = None
            item.last_error = None
            item.response_preview = None

    def _start_fetch(self) -> None:
        if not self.provider.base_url:
            QMessageBox.warning(self, "缺少 Base URL", "请先配置 Provider Base URL")
            return
        if self.fetch_worker and self.fetch_worker.isRunning():
            return

        self.fetch_button.setEnabled(False)
        self.fetch_button.setText("拉取中…")
        self.progress_label.setText(f"正在拉取 {self.provider.name} 的模型清单…")

        self.fetch_worker = FetchWorker(self.provider, self)
        self.fetch_worker.signals.succeeded.connect(self._on_fetch_succeeded)
        self.fetch_worker.signals.failed.connect(self._on_fetch_failed)
        self.fetch_worker.finished.connect(self._on_fetch_finished)
        self.fetch_worker.start()

    def _on_fetch_succeeded(self, ids: list[str]) -> None:
        added = self._merge_items(ids)
        if added == 0:
            QMessageBox.information(
                self, "拉取完成",
                f"未发现新模型（已存在 {len(self.items)} 个）。该 provider 返回了 {len(ids)} 个 id。"
            )
        else:
            QMessageBox.information(
                self, "拉取完成",
                f"新增 {added} 个模型，当前共 {len(self.items)} 个（provider 返回 {len(ids)} 个）。"
            )
        self.progress_label.setText(
            f"模型清单已更新 · 当前共 {len(self.items)} 个，待测试"
        )

    def _on_fetch_failed(self, error: str) -> None:
        QMessageBox.warning(
            self, "拉取失败",
            f"无法从 provider 拉取模型清单：\n{error[:500]}\n\n将保留内置清单。"
        )
        self.progress_label.setText(
            f"拉取失败，使用内置清单 · 当前 {len(self.items)} 个，待测试"
        )

    def _on_fetch_finished(self) -> None:
        self.fetch_button.setEnabled(True)
        self.fetch_button.setText("拉取模型")

    def _reset_model_list(self) -> None:
        """手动清空当前 Provider 的模型清单，恢复初始清单。

        初始清单 = NVIDIA 官方端点的内置 models.json；其他 Provider 为空。
        清除该 Provider 下「拉取」得到的模型与全部测试状态。
        """
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "测试进行中", "请先停止测试再重置列表。")
            return
        if self.fetch_worker and self.fetch_worker.isRunning():
            QMessageBox.warning(self, "正在拉取模型", "请等待拉取完成后再重置列表。")
            return
        removed = len(self.items)
        self.items = self._initial_items_for(self.provider)
        self.visible_items = list(self.items)
        self.table.set_items(self.visible_items)
        self.progress_label.setText(
            f"已重置列表（移除 {removed} 个，当前 {len(self.items)} 个），待测试"
        )

    def _load_builtin_catalog(self) -> None:
        """手动把内置的 NVIDIA NIM 模型清单并入当前 Provider 的清单（去重，不覆盖已有项）。

        适用于第三方 Provider 想顺带测某些内置模型 ID 的场景。
        """
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "测试进行中", "请先停止测试再加载内置清单。")
            return
        if self.fetch_worker and self.fetch_worker.isRunning():
            QMessageBox.warning(self, "正在拉取模型", "请等待拉取完成后再加载内置清单。")
            return
        builtin = model_store.load_model_items()
        if not builtin:
            QMessageBox.warning(
                self, "内置清单缺失",
                (model_store.LAST_LOAD_ERROR or "内置清单为空")
                + "\n\n可点击「拉取模型」从 Provider 获取清单。",
            )
            return
        added = self._merge_items([item.id for item in builtin])
        if added == 0:
            self.progress_label.setText(
                f"内置清单已全部存在（当前共 {len(self.items)} 个），无新增"
            )
        else:
            self.progress_label.setText(
                f"已并入内置清单：新增 {added} 个，当前共 {len(self.items)} 个，待测试"
            )

    def _merge_items(self, ids: list[str]) -> int:
        """把 provider 返回的 id 合并进 self.items（去重），新增项默认勾选。

        返回新增数量。
        """
        existing = {item.id for item in self.items}
        added = 0
        for raw in ids:
            mid = str(raw).strip()
            if not mid or mid in existing:
                continue
            if "/" in mid:
                vendor, name = mid.split("/", 1)
            else:
                vendor, name = "", mid
            self.items.append(ModelItem(id=mid, vendor=vendor, name=name, selected=True))
            existing.add(mid)
            added += 1
        if added:
            self.visible_items = list(self.items)
            self.table.set_items(self.visible_items)
        return added

    # ---------- 新功能：快捷键 / 复制 / 导出 / 清空 ----------
    def _setup_shortcuts(self) -> None:
        """注册全局快捷键。

        Ctrl+A / D / I          选取（全选/全不选/反选）
        Delete                  清空所有测试结果
        Ctrl+Shift+C            复制成功模型 ID 到剪贴板
        Ctrl+Shift+E            导出全部结果到 CSV
        Ctrl+Shift+R            拉取模型清单
        Ctrl+Shift+L            重置模型清单（恢复初始清单）
        Ctrl+Shift+B            加载内置模型清单（并入当前）
        Ctrl+T                  开始/停止测试（按当前状态切换）
        """
        # 选择类快捷键挂在表格上（WidgetWithChildrenShortcut），
        # 避免劫持“筛选模型”输入框里的 Ctrl+A 全选文本 / Delete 删字符
        self._shortcut_select_all = QShortcut(QKeySequence("Ctrl+A"), self.table)
        self._shortcut_select_all.setContext(Qt.WidgetWithChildrenShortcut)
        self._shortcut_select_all.activated.connect(lambda: self.table.check_all(True))

        self._shortcut_select_none = QShortcut(QKeySequence("Ctrl+D"), self.table)
        self._shortcut_select_none.setContext(Qt.WidgetWithChildrenShortcut)
        self._shortcut_select_none.activated.connect(lambda: self.table.check_all(False))

        self._shortcut_select_invert = QShortcut(QKeySequence("Ctrl+I"), self.table)
        self._shortcut_select_invert.setContext(Qt.WidgetWithChildrenShortcut)
        self._shortcut_select_invert.activated.connect(self.table.invert_selection)

        self._shortcut_clear = QShortcut(QKeySequence("Delete"), self.table)
        self._shortcut_clear.setContext(Qt.WidgetWithChildrenShortcut)
        self._shortcut_clear.activated.connect(self._clear_results)

        self._shortcut_copy = QShortcut(QKeySequence("Ctrl+Shift+C"), self)
        self._shortcut_copy.activated.connect(self._copy_success_ids)

        self._shortcut_export = QShortcut(QKeySequence("Ctrl+Shift+E"), self)
        self._shortcut_export.activated.connect(self._export_csv)

        self._shortcut_fetch = QShortcut(QKeySequence("Ctrl+Shift+R"), self)
        self._shortcut_fetch.activated.connect(self._start_fetch)

        self._shortcut_reset_list = QShortcut(QKeySequence("Ctrl+Shift+L"), self)
        self._shortcut_reset_list.activated.connect(self._reset_model_list)

        self._shortcut_builtin = QShortcut(QKeySequence("Ctrl+Shift+B"), self)
        self._shortcut_builtin.activated.connect(self._load_builtin_catalog)

        self._shortcut_toggle = QShortcut(QKeySequence("Ctrl+T"), self)
        self._shortcut_toggle.activated.connect(self._toggle_test)

    def _toggle_test(self) -> None:
        """Ctrl+T：根据当前状态切换开始/停止。"""
        if self.worker and self.worker.isRunning():
            self._stop_test()
        else:
            self._start_test()

    def _copy_success_ids(self) -> None:
        """把 status==success 的模型 ID 拼成多行文本，复制到剪贴板。"""
        successful = self.table.successful_items()
        if not successful:
            QMessageBox.information(self, "无可用模型", "当前没有状态为\"成功\"的模型可复制。")
            return
        from PySide6.QtGui import QGuiApplication
        text = "\n".join(it.id for it in successful)
        QGuiApplication.clipboard().setText(text)
        self.progress_label.setText(f"已复制 {len(successful)} 个可用模型 ID 到剪贴板")

    def _export_csv(self) -> None:
        """导出全部结果到 CSV。"""
        if not self.items:
            QMessageBox.information(self, "无数据", "模型清单为空，无可导出内容。")
            return
        default_name = f"model_test_{self.provider.name}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出测试结果", default_name, "CSV 文件 (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "provider", "model_id", "vendor", "name",
                    "status", "latency_ms", "streaming",
                    "error", "response_preview",
                ])
                for it in self.items:
                    writer.writerow([
                        self.provider.name, it.id, it.vendor, it.name,
                        it.status,
                        it.latency_ms if it.latency_ms is not None else "",
                        "" if it.streaming_supported is None
                        else ("是" if it.streaming_supported else "否"),
                        it.last_error or "",
                        it.response_preview or "",
                    ])
            self.progress_label.setText(f"已导出 {len(self.items)} 条结果到 {path}")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"无法写入文件：{exc}")

    def _clear_results(self, silent: bool = False) -> None:
        """清空所有测试状态。silent=True 时不弹提示框（用于内部调用如切换 provider）。"""
        if not silent and self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "测试进行中", "请先停止测试再清空结果。")
            return
        if not silent and self._has_results():
            answer = QMessageBox.question(
                self, "清空结果",
                "确定要清空所有模型的测试结果吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.table.clear_results()
        # 重新同步可见列表（_filter_items 会自动算）
        if self.search_edit.text():
            self._filter_items(self.search_edit.text())
        else:
            self.visible_items = list(self.items)
            self.table.set_items(self.visible_items)
        if not silent:
            self.progress_label.setText(f"已清空测试结果，待测试 {len(self.items)} 个模型")

    def _has_results(self) -> bool:
        return any(
            it.status not in ("pending", "") or it.latency_ms is not None
            for it in self.items
        )
