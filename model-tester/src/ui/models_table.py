from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
)

from models import ModelItem


class ModelsTable(QTableWidget):
    HEADERS = [
        "",
        "模型",
        "状态",
        "延迟(ms)",
        "流式",
        "错误",
        "响应片段",
    ]

    # 状态文字 → 排序权重（pending/running 放最前，failed/skipped 放最后）
    STATUS_ORDER = {
        "success": 0,
        "pending": 1,
        "running": 2,
        "skipped": 3,
        "failed": 4,
    }

    def __init__(self, parent=None):
        super().__init__(0, len(self.HEADERS), parent)
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.horizontalHeader().setSectionsClickable(True)
        self.horizontalHeader().setSortIndicatorShown(True)
        self.setSortingEnabled(False)  # 我们自己管理排序，避免与 item 状态更新冲突

        # 当前排序列：-1 表示未排序（保持原序）
        self._sort_column: int = -1
        self._sort_order: Qt.SortOrder = Qt.AscendingOrder
        self.horizontalHeader().sectionClicked.connect(self._on_header_clicked)

        # 内部存一份 (item, row) 索引，方便排序后 update_row 找对行
        self._items: list[ModelItem] = []

    # ---------- 数据层 ----------
    def set_items(self, items: list[ModelItem]) -> None:
        self._items = list(items)
        self._render()

    def update_item(self, item: ModelItem) -> None:
        """按对象身份更新一行。

        test_model 原地修改并返回同一 ModelItem 对象，因此用 is 身份比较定位，
        避免 dataclass 值相等比较在两条字段完全相同的行上命中错误目标。
        """
        for row, existing in enumerate(self._items):
            if existing is item:
                self._fill_row(row, item)
                return

    def selected_ids(self) -> list[str]:
        ids: list[str] = []
        for row in range(self.rowCount()):
            check_item = self.item(row, 0)
            if check_item and check_item.checkState() == Qt.Checked:
                ids.append(str(self.item(row, 1).text()))
        return ids

    def check_all(self, checked: bool) -> None:
        for row in range(self.rowCount()):
            check_item = self.item(row, 0)
            if check_item:
                check_item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def invert_selection(self) -> None:
        for row in range(self.rowCount()):
            check_item = self.item(row, 0)
            if check_item:
                cur = check_item.checkState()
                check_item.setCheckState(Qt.Unchecked if cur == Qt.Checked else Qt.Checked)

    def successful_items(self) -> list[ModelItem]:
        return [it for it in self._items if it.status == "success"]

    def clear_results(self) -> None:
        """清空所有 item 的测试状态。"""
        for it in self._items:
            it.status = "pending"
            it.latency_ms = None
            it.streaming_supported = None
            it.last_error = None
            it.response_preview = None
        self._render()

    # ---------- 渲染 ----------
    def _render(self) -> None:
        self.setRowCount(len(self._items))
        for row, item in enumerate(self._items):
            self._fill_row(row, item)

    def _fill_row(self, row: int, item: ModelItem) -> None:
        # col 0: checkbox
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        check.setCheckState(Qt.Checked if item.selected else Qt.Unchecked)
        check.setData(Qt.UserRole, item.id)
        self.setItem(row, 0, check)

        # col 1: 模型 id
        c1 = QTableWidgetItem(item.id)
        c1.setData(Qt.UserRole, item.id.lower())   # 排序按小写
        self.setItem(row, 1, c1)

        # col 2: 状态
        c2 = QTableWidgetItem(item.status)
        c2.setData(Qt.UserRole, self.STATUS_ORDER.get(item.status, 99))
        self.setItem(row, 2, c2)

        # col 3: 延迟
        latency_text = str(item.latency_ms) if item.latency_ms is not None else ""
        c3 = QTableWidgetItem(latency_text)
        c3.setData(Qt.UserRole, item.latency_ms if item.latency_ms is not None else float("inf"))
        self.setItem(row, 3, c3)

        # col 4: 流式
        stream_text = self._stream_text(item)
        c4 = QTableWidgetItem(stream_text)
        # 排序：是 > 否 > 空
        c4.setData(Qt.UserRole, {
            True: 0, False: 1, None: 2
        }.get(item.streaming_supported, 2))
        self.setItem(row, 4, c4)

        # col 5: 错误
        c5 = QTableWidgetItem(item.last_error or "")
        c5.setData(Qt.UserRole, (item.last_error or "").lower())
        self.setItem(row, 5, c5)

        # col 6: 响应片段
        c6 = QTableWidgetItem(item.response_preview or "")
        c6.setData(Qt.UserRole, (item.response_preview or "").lower())
        self.setItem(row, 6, c6)

    # ---------- 排序 ----------
    def _on_header_clicked(self, column: int) -> None:
        # 不让用户对 checkbox 列排序
        if column == 0:
            return
        if self._sort_column == column:
            # 再次点同一列 -> 反转顺序
            self._sort_order = (Qt.DescendingOrder
                                if self._sort_order == Qt.AscendingOrder
                                else Qt.AscendingOrder)
        else:
            self._sort_column = column
            self._sort_order = Qt.AscendingOrder
        self._apply_sort()

    def _apply_sort(self) -> None:
        if self._sort_column < 0:
            return
        col = self._sort_column
        reverse = (self._sort_order == Qt.DescendingOrder)

        def sort_key(it: ModelItem):
            v = self._sort_value(it, col)
            # None / inf 等异常值统一丢到队尾
            if v is None:
                return (1, "")
            if v == float("inf"):
                return (1, "")
            return (0, v)

        self._items.sort(key=sort_key, reverse=reverse)
        self.horizontalHeader().setSortIndicator(col, self._sort_order)
        self._render()

    def _sort_value(self, item: ModelItem, col: int):
        if col == 1:
            return item.id.lower()
        if col == 2:
            return self.STATUS_ORDER.get(item.status, 99)
        if col == 3:
            return item.latency_ms if item.latency_ms is not None else float("inf")
        if col == 4:
            return {True: 0, False: 1, None: 2}.get(item.streaming_supported, 2)
        if col == 5:
            return (item.last_error or "").lower()
        if col == 6:
            return (item.response_preview or "").lower()
        return ""

    def _stream_text(self, item: ModelItem) -> str:
        if item.streaming_supported is None:
            return ""
        return "是" if item.streaming_supported else "否"
