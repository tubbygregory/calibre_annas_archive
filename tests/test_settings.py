import json
import tempfile
import unittest
from pathlib import Path

from tests.support import BACKUP, MIRROR

from calibre_plugins.store_annas_archive.constants import DEFAULT_MIRRORS, LEGACY_MIRRORS
from calibre_plugins.store_annas_archive.settings import (
    cache_seconds, configured_mirrors, link_options, save_preferences, search_options,
)


class FilePreferences(dict):
    def __init__(self, path):
        self.path = path
        super().__init__(json.loads(path.read_text(encoding='utf-8')) if path.exists() else {})

    def __enter__(self):
        pass

    def __exit__(self, *error):
        self.path.write_text(json.dumps(self), encoding='utf-8')


class SettingsTest(unittest.TestCase):
    def test_only_legacy_default_mirrors_are_replaced(self):
        self.assertEqual(configured_mirrors({}), DEFAULT_MIRRORS)
        self.assertEqual(configured_mirrors({'mirrors': list(LEGACY_MIRRORS)}), DEFAULT_MIRRORS)
        self.assertEqual(configured_mirrors({'mirrors': list(reversed(LEGACY_MIRRORS))}), LEGACY_MIRRORS[::-1])
        self.assertEqual(configured_mirrors({'mirrors': []}), ())
        self.assertEqual(configured_mirrors({'mirrors': MIRROR + '/'}), (MIRROR,))
        self.assertEqual(configured_mirrors({'mirrors': ['file:///etc/passwd', MIRROR, MIRROR + '/', BACKUP]}),
                         (MIRROR, BACKUP))

    def test_preference_roundtrip_preserves_legacy_keys_and_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.json'
            previous = {'open_external': False, 'tags': 'archive', 'search': {'order': 'oldest', 'future': 'keep'},
                        'link': {'url_extension': True, 'future': True}, 'other_store_key': 'keep this too'}
            path.write_text(json.dumps(previous), encoding='utf-8')
            prefs = FilePreferences(path)
            save_preferences(prefs, mirrors=[BACKUP, MIRROR], search={'order': 'smallest', 'language': ['en', 'nl-BE']},
                             link={'url_extension': False, 'content_type': True}, open_external=True, prefer_epub=True,
                             cache_seconds=180, tags='archive, books')
            loaded = FilePreferences(path)
            self.assertEqual(configured_mirrors(loaded), (BACKUP, MIRROR))
            self.assertEqual(search_options(loaded)['language'], ('en', 'nl-BE'))
            self.assertEqual(search_options(loaded)['order'], 'smallest')
            self.assertEqual(link_options(loaded), {'url_extension': False, 'content_type': True})
            self.assertEqual(cache_seconds(loaded), 180)
            self.assertEqual(loaded['tags'], 'archive, books')
            self.assertTrue(loaded['open_external'])
            self.assertTrue(loaded['prefer_epub'])
            self.assertEqual(loaded['search']['future'], 'keep')
            self.assertTrue(loaded['link']['future'])
            self.assertEqual(loaded['other_store_key'], 'keep this too')

    def test_malformed_preferences_use_safe_values(self):
        self.assertEqual(search_options({'search': {'order': 'unsupported'}})['order'], '')
        self.assertEqual(search_options({'search': {'language': 'en', 'source': [None, {}, 'lgli']}})['source'], ('lgli',))
        self.assertEqual(link_options({'link': []}), {'url_extension': True, 'content_type': False})
        self.assertEqual(cache_seconds({'cache_seconds': 'oops'}), cache_seconds({}))
        self.assertEqual(cache_seconds({'cache_seconds': -99}), 0)
        self.assertEqual(cache_seconds({'cache_seconds': 99_999}), 3600)
