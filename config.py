"""Calibre preferences for the Archive store."""

import logging

from calibre.gui2 import open_url

from .constants import DEFAULT_MIRRORS, FILTERS, MAX_CACHE_SECONDS, ORDER
from .http_client import ArchiveError, mirror_url
from .settings import cache_seconds, configured_mirrors, link_options, save_preferences, search_options

try:
    from qt.core import (
        QCheckBox, QComboBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
        QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QObject, QPushButton, QRunnable,
        QSpinBox, QTabWidget, QThreadPool, QTreeWidget, QTreeWidgetItem, QUrl, QVBoxLayout, QWidget, Qt,
        pyqtSignal, pyqtSlot,
    )
except ImportError:
    from PyQt5.Qt import (
        QCheckBox, QComboBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
        QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QObject, QPushButton, QRunnable,
        QSpinBox, QTabWidget, QThreadPool, QTreeWidget, QTreeWidgetItem, QUrl, QVBoxLayout, QWidget, Qt,
        pyqtSignal, pyqtSlot,
    )

logger = logging.getLogger(__name__)


class MirrorProbeSignals(QObject):
    finished = pyqtSignal(str, str)


class MirrorProbe(QRunnable):
    def __init__(self, backend, mirror):
        super().__init__()
        self.backend = backend
        self.mirror = mirror
        self.signals = MirrorProbeSignals()

    def run(self):
        try:
            self.backend.test_mirror(self.mirror)
        except ArchiveError as error:
            self.signals.finished.emit(self.mirror, str(error))
        except Exception as error:
            logger.error('Anna’s Archive mirror test [application]: %s', type(error).__name__)
            self.signals.finished.emit(self.mirror, _('Unexpected error. See the Calibre log.'))
        else:
            self.signals.finished.emit(self.mirror, _('Search page verified'))


class ConfigWidget(QWidget):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.testing = set()
        self.resize(690, 480)
        layout = QVBoxLayout(self)
        tabs = QTabWidget(self)
        layout.addWidget(tabs)
        tabs.addTab(self._search_tab(), _('Search'))
        tabs.addTab(self._mirrors_tab(), _('Mirrors'))
        tabs.addTab(self._downloads_tab(), _('Downloads'))
        self.load_settings()

    def _search_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.order = QComboBox()
        for label, value in ORDER:
            self.order.addItem(_(label), value)
        form.addRow(_('Sort by'), self.order)
        layout.addLayout(form)

        grid = QGridLayout()
        self.filters = {}
        for index, field in enumerate(FILTERS):
            box = QGroupBox(_(field.label))
            controls = QListWidget()
            controls.setMinimumHeight(105)
            self.filters[field.key] = controls
            for label, value in field.choices:
                item = QListWidgetItem(_(label), controls)
                item.setData(Qt.ItemDataRole.UserRole, value)
                item.setCheckState(Qt.CheckState.Unchecked)
            QVBoxLayout(box).addWidget(controls)
            grid.addWidget(box, index // 2, index % 2)
        layout.addLayout(grid)

        self.prefer_epub = QCheckBox(_('Prefer EPUB within each search page'))
        self.expiry = QSpinBox()
        self.expiry.setRange(0, MAX_CACHE_SECONDS)
        self.expiry.setSpecialValueText(_('Disabled'))
        self.expiry.setSuffix(_(' seconds'))
        form = QFormLayout()
        form.addRow(self.prefer_epub)
        form.addRow(_('Search cache expiry'), self.expiry)
        layout.addLayout(form)
        return page

    def _mirrors_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel(_('Calibre tries mirrors in this order. Use only addresses you trust.'))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.mirrors = QTreeWidget()
        self.mirrors.setHeaderLabels([_('Mirror'), _('Status')])
        self.mirrors.setRootIsDecorated(False)
        self.mirrors.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.mirrors)

        buttons = QHBoxLayout()
        for label, action in (
            (_('Add'), self._add_mirror),
            (_('Remove'), self._remove_mirror),
            (_('Move up'), lambda: self._move_mirror(-1)),
            (_('Move down'), lambda: self._move_mirror(1)),
            (_('Reset defaults'), lambda: self._set_mirrors(DEFAULT_MIRRORS)),
        ):
            self._button(buttons, label, action)
        layout.addLayout(buttons)

        buttons = QHBoxLayout()
        self._button(buttons, _('Test selected mirror'), self._test_mirror)
        self._button(buttons, _('Open in browser'), self._browse_mirror)
        buttons.addStretch()
        layout.addLayout(buttons)
        return page

    @staticmethod
    def _button(layout, label, action):
        button = QPushButton(label)
        button.clicked.connect(action)
        layout.addWidget(button)

    def _downloads_tab(self):
        page = QWidget()
        layout = QFormLayout(page)
        self.url_extension = QCheckBox(_('Check recognised URL extensions'))
        self.url_extension.setToolTip(_('Reject links whose final URL has a conflicting ebook extension.'))
        self.content_type = QCheckBox(_('Require a compatible Content-Type'))
        self.content_type.setToolTip(_('Require a file or binary Content-Type in addition to a matching file signature.'))
        self.open_external = QCheckBox(_('Open store in external web browser'))
        self.tags = QLineEdit()
        layout.addRow(self.url_extension)
        layout.addRow(self.content_type)
        layout.addRow(self.open_external)
        layout.addRow(_('Tags for books downloaded in the store window'), self.tags)
        return page

    def _mirror_items(self):
        return [self.mirrors.topLevelItem(index) for index in range(self.mirrors.topLevelItemCount())]

    def _set_mirrors(self, mirrors):
        self.mirrors.clear()
        for mirror in mirrors:
            self._append_mirror(mirror)

    def _append_mirror(self, mirror):
        status = _('Checking…') if mirror in self.testing else _('Not tested')
        item = QTreeWidgetItem([mirror, status])
        self.mirrors.addTopLevelItem(item)
        self.mirrors.setCurrentItem(item)

    def _add_mirror(self):
        value, accepted = QInputDialog.getText(self, _('Add an Anna’s Archive mirror'), _('Mirror URL'))
        if not accepted:
            return
        mirror = mirror_url(value)
        if not mirror:
            QMessageBox.warning(self, _('Invalid mirror'), _('Enter a public HTTP or HTTPS address without a path.'))
        elif mirror not in [item.text(0) for item in self._mirror_items()]:
            self._append_mirror(mirror)

    def _remove_mirror(self):
        item = self.mirrors.currentItem()
        if item is not None:
            self.mirrors.takeTopLevelItem(self.mirrors.indexOfTopLevelItem(item))

    def _move_mirror(self, offset):
        item = self.mirrors.currentItem()
        if item is None:
            return
        index = self.mirrors.indexOfTopLevelItem(item)
        target = index + offset
        if index >= 0 and 0 <= target < self.mirrors.topLevelItemCount():
            self.mirrors.insertTopLevelItem(target, self.mirrors.takeTopLevelItem(index))
            self.mirrors.setCurrentItem(item)

    def _browse_mirror(self):
        item = self.mirrors.currentItem()
        if item is not None:
            open_url(QUrl(item.text(0)))

    def _test_mirror(self):
        item = self.mirrors.currentItem()
        if item is None or item.text(0) in self.testing:
            return
        mirror = item.text(0)
        self.testing.add(mirror)
        item.setText(1, _('Checking…'))
        task = MirrorProbe(self.store.backend, mirror)
        task.signals.finished.connect(self._show_probe_result)
        QThreadPool.globalInstance().start(task)

    @pyqtSlot(str, str)
    def _show_probe_result(self, mirror, status):
        self.testing.discard(mirror)
        for item in self._mirror_items():
            if item.text(0) == mirror:
                item.setText(1, status)
                item.setToolTip(1, status)
                break

    def load_settings(self):
        config = self.store.config
        selected = search_options(config)
        self.order.setCurrentIndex(self.order.findData(selected['order']))
        for key, controls in self.filters.items():
            for index in range(controls.count()):
                item = controls.item(index)
                checked = item.data(Qt.ItemDataRole.UserRole) in selected[key]
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.prefer_epub.setChecked(config.get('prefer_epub', False) is True)
        self.expiry.setValue(cache_seconds(config))
        self._set_mirrors(configured_mirrors(config))
        link = link_options(config)
        self.url_extension.setChecked(link['url_extension'])
        self.content_type.setChecked(link['content_type'])
        self.open_external.setChecked(bool(config.get('open_external', False)))
        self.tags.setText(str(config.get('tags', '')))

    def save_settings(self):
        search = {'order': self.order.currentData()}
        for key, controls in self.filters.items():
            items = (controls.item(index) for index in range(controls.count()))
            search[key] = [
                item.data(Qt.ItemDataRole.UserRole) for item in items
                if item.checkState() == Qt.CheckState.Checked
            ]
        save_preferences(
            self.store.config, mirrors=[item.text(0) for item in self._mirror_items()], search=search,
            link={'url_extension': self.url_extension.isChecked(), 'content_type': self.content_type.isChecked()},
            open_external=self.open_external.isChecked(), prefer_epub=self.prefer_epub.isChecked(),
            cache_seconds=self.expiry.value(), tags=self.tags.text(),
        )
