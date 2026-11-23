"""Website search parameter names and choices shared by the backend and settings."""

from dataclasses import dataclass

# See the site's FAQ for the published mirror list. Domains can change.
DEFAULT_MIRRORS = (
    'https://annas-archive.gl',
    'https://annas-archive.pk',
    'https://annas-archive.gd',
)
LEGACY_MIRRORS = (
    'https://annas-archive.org',
    'https://annas-archive.li',
    'https://annas-archive.se',
)
FORMATS = ('EPUB', 'MOBI', 'PDF', 'AZW3', 'CBR', 'CBZ', 'FB2', 'DJVU', 'TXT', 'AZW', 'RTF')
DEFAULT_CACHE_SECONDS = 300
MAX_CACHE_SECONDS = 3600

ORDER = (
    ('Most relevant', ''),
    ('Newest (publication year)', 'newest'),
    ('Oldest (publication year)', 'oldest'),
    ('Largest', 'largest'),
    ('Smallest', 'smallest'),
    ('Newest added', 'newest_added'),
    ('Oldest added', 'oldest_added'),
)

LANGUAGES = {
    'Unknown language': '_empty', 'English': 'en', 'Spanish': 'es', 'Italian': 'it', 'Portuguese': 'pt', 'French': 'fr',
    'German': 'de', 'Chinese': 'zh', 'Turkish': 'tr', 'Dutch': 'nl', 'Hungarian': 'hu', 'Catalan': 'ca',
    'Romanian': 'ro', 'Russian': 'ru', 'Czech': 'cs', 'Lithuanian': 'lt', 'Greek': 'el', 'Polish': 'pl', 'Danish': 'da',
    'Croatian': 'hr', 'Korean': 'ko', 'Hindi': 'hi', 'Japanese': 'ja', 'Latvian': 'lv', 'Latin': 'la',
    'Indonesian': 'id', 'Swedish': 'sv', 'Hebrew': 'he', 'Bangla': 'bn', 'Norwegian': 'no', 'Ukrainian': 'uk',
    'Luxembourgish': 'lb', 'Arabic': 'ar', 'Irish': 'ga', 'Welsh': 'cy', 'Bulgarian': 'bg', 'Tamil': 'ta',
    'Traditional Chinese': 'zh-Hant', 'Afrikaans': 'af', 'Persian': 'fa', 'Serbian': 'sr', 'Belarusian': 'be',
    'Dongxiang': 'sce', 'Vietnamese': 'vi', 'Urdu': 'ur', 'Flemish': 'nl-BE', 'Ndolo': 'ndl', 'Kazakh': 'kk',
}


@dataclass(frozen=True)
class SearchFilter:
    key: str
    parameter: str
    label: str
    choices: tuple


FILTERS = (
    SearchFilter('content', 'content', 'Content', (
        ('Book (non-fiction)', 'book_nonfiction'),
        ('Book (fiction)', 'book_fiction'),
        ('Book (unknown)', 'book_unknown'),
        ('Magazine', 'magazine'),
        ('Comic book', 'book_comic'),
        ('Standards document', 'standards_document'),
        ('Other', 'other'),
        ('Musical score', 'musical_score'),
        ('Audiobook', 'audiobook'),
    )),
    SearchFilter('filetype', 'ext', 'File format', tuple((fmt, fmt.lower()) for fmt in FORMATS)),
    SearchFilter('access', 'acc', 'Access', (
        ('Partner server download', 'aa_download'),
        ('External download', 'external_download'),
        ('External borrow', 'external_borrow'),
        ('External borrow (print disabled)', 'external_borrow_printdisabled'),
        ('Contained in torrents', 'torrents_available'),
    )),
    SearchFilter('source', 'src', 'Source', (
        ('LibGen.li', 'lgli'),
        ('LibGen.rs', 'lgrs'),
        ('Sci-Hub', 'scihub'),
        ('Z-Library', 'zlib'),
        ('Internet Archive', 'ia'),
        ('Uploads to AA', 'upload'),
        ('Nexus/STC', 'nexusstc'),
        ('DuXiu', 'duxiu'),
        ('Z-Library Chinese', 'zlibzh'),
        ('MagzDB', 'magzdb'),
    )),
    SearchFilter('language', 'lang', 'Language', tuple(
        (name if code == '_empty' else f'{name} [{code}]', code)
        for name, code in LANGUAGES.items()
    )),
)
