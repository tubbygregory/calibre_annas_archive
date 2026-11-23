"""Test the backend as a package without needing the Calibre ZIP importer."""

import io
import sys
from email.message import Message
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'calibre_plugins.store_annas_archive'
calibre_plugins = ModuleType('calibre_plugins')
calibre_plugins.__path__ = []
plugin = ModuleType(PACKAGE)
plugin.__path__ = [str(ROOT)]
sys.modules.setdefault('calibre_plugins', calibre_plugins)
sys.modules.setdefault(PACKAGE, plugin)

MIRROR = 'https://mirror-one.example'
BACKUP = 'https://mirror-two.example'
PDF = b'%PDF-1.6\nSynthetic fixture'
EPUB = b'PK\x03\x04' + b'\x00' * 26 + b'mimetypeapplication/epub+zip'


def fixture(name):
    return (ROOT / 'tests' / 'fixtures' / name).read_bytes()


class FakeResponse(io.BytesIO):
    def __init__(self, url, body=b'', *, code=200, content_type='text/html; charset=utf-8', headers=None):
        super().__init__(body)
        self.url = url
        self.code = code
        self.headers = Message()
        if content_type:
            self.headers['Content-Type'] = content_type
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def geturl(self):
        return self.url
