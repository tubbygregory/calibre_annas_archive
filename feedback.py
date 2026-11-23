"""Show Archive search failures in Calibre's Get books window."""

from itertools import count
from weakref import ref

from calibre.gui2 import open_url

try:
    from qt.core import (
        QApplication, QFrame, QHBoxLayout, QLabel, QObject, QPushButton, Qt, QUrl, pyqtSignal, pyqtSlot,
    )
except ImportError:
    from PyQt5.Qt import (
        QApplication, QFrame, QHBoxLayout, QLabel, QObject, QPushButton, Qt, QUrl, pyqtSignal, pyqtSlot,
    )


class SearchWarning(QFrame):
    name = 'annas_archive_search_warning'

    def __init__(self, dialog):
        super().__init__(dialog)
        self.setObjectName(self.name)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(self)
        self.message = QLabel(self)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message, 1)
        self.browser = QPushButton(_('Open in browser'), self)
        self.browser.clicked.connect(self._open)
        layout.addWidget(self.browser)
        self.url = ''
        dialog.layout().addWidget(self)
        search_button = getattr(dialog, 'search', None)
        if search_button is not None:
            search_button.clicked.connect(self.hide)

    def _open(self):
        if self.url:
            open_url(QUrl(self.url))

    def report(self, message, url):
        self.url = url
        self.message.setText(message)
        self.browser.setVisible(bool(url))
        self.setVisible(bool(message))


class SearchFeedback(QObject):
    changed = pyqtSignal(int, str, str)

    def __init__(self):
        super().__init__()
        self.searches = count(1)
        self.serial = 0
        self.dialog = None
        self.changed.connect(self._display, Qt.ConnectionType.QueuedConnection)

    def start(self):
        number = next(self.searches)
        self.changed.emit(number, '', '')
        return number

    @staticmethod
    def _active_dialog():
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            return None
        from calibre.gui2.store.search.search import SearchDialog

        for window in (app.activeModalWidget(), app.activeWindow()):
            while window is not None:
                if isinstance(window, SearchDialog):
                    return window
                window = window.parentWidget()
        return None

    @pyqtSlot(int, str, str)
    def _display(self, number, message, url):
        if number < self.serial:
            return
        active = self._active_dialog()
        if number > self.serial:
            self.serial = number
            self.dialog = ref(active) if active is not None else None
        if active is None or self.dialog is None or self.dialog() is not active:
            return
        warning = active.findChild(SearchWarning, SearchWarning.name)
        if warning is None:
            if not message or active.layout() is None:
                return
            warning = SearchWarning(active)
        warning.report(message, url)
