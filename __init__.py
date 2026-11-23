from calibre.customize import StoreBase


class AnnasArchiveStore(StoreBase):
    name = "Anna's Archive"
    description = "Search Anna's Archive from Calibre's Get books window."
    supported_platforms = ['windows', 'osx', 'linux']
    author = 'ScottBot10'
    version = (0, 3, 0)
    minimum_calibre_version = (5, 0, 0)
    # Calibre can reload this wrapper while upgrading from a ZIP with older submodules.
    formats = ['EPUB', 'MOBI', 'PDF', 'AZW3', 'CBR', 'CBZ', 'FB2', 'DJVU', 'TXT', 'AZW', 'RTF']

    actual_plugin = 'calibre_plugins.store_annas_archive.annas_archive:AnnasArchiveStore'

    def is_customizable(self):
        return True
