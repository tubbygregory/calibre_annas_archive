import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from build_plugin import FILES, ROOT, build, plugin_version


class BuildTest(unittest.TestCase):
    def test_reproducible_zip_with_plugin_files_at_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = build(root / 'first')
            second = build(root / 'second')
            self.assertEqual(first.name, f'calibre_annas_archive-v{plugin_version()}.zip')
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with ZipFile(first) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(set(archive.namelist()), set(FILES))
                self.assertEqual(archive.read('plugin-import-name-store_annas_archive.txt'), b'')
                self.assertEqual(archive.read('__init__.py'), (ROOT / '__init__.py').read_bytes())
                self.assertFalse(any('/' in name for name in archive.namelist()))
