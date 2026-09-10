from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QDialogButtonBox,
    QVBoxLayout,
)

from providers import Provider


class ProviderDialog(QDialog):
    def __init__(self, provider: Provider, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Provider 配置")
        self.provider = provider

        self.name_edit = QLineEdit(provider.name)
        self.base_url_edit = QLineEdit(provider.base_url)
        self.api_key_edit = QLineEdit(provider.api_key)
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1, 600)
        self.timeout_spin.setValue(provider.timeout_seconds)
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 20)
        self.workers_spin.setValue(provider.max_workers)

        form = QFormLayout()
        form.addRow("名称", self.name_edit)
        form.addRow("Base URL", self.base_url_edit)
        form.addRow("API Key", self.api_key_edit)
        form.addRow("超时秒数", self.timeout_spin)
        form.addRow("并发数", self.workers_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        container = QVBoxLayout()
        container.addLayout(form)
        container.addWidget(buttons)
        self.setLayout(container)

    def result_provider(self) -> Provider:
        return Provider(
            name=self.name_edit.text().strip() or "Nvidia",
            base_url=self.base_url_edit.text().strip(),
            api_key=self.api_key_edit.text().strip(),
            timeout_seconds=self.timeout_spin.value(),
            max_workers=self.workers_spin.value(),
        )
