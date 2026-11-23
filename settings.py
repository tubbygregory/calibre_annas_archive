"""Read legacy Calibre preferences and save the current settings format."""

from contextlib import nullcontext

from .constants import DEFAULT_CACHE_SECONDS, DEFAULT_MIRRORS, FILTERS, LEGACY_MIRRORS, MAX_CACHE_SECONDS, ORDER
from .http_client import mirror_url


def _mapping(value):
    return value if isinstance(value, dict) else {}


def configured_mirrors(config):
    saved = config.get('mirrors', DEFAULT_MIRRORS)
    if isinstance(saved, str):
        saved = (saved,)
    if not isinstance(saved, (tuple, list)):
        return DEFAULT_MIRRORS
    mirrors = tuple(dict.fromkeys(filter(None, map(mirror_url, saved))))
    return DEFAULT_MIRRORS if mirrors == LEGACY_MIRRORS else mirrors


def search_options(config):
    saved = _mapping(config.get('search'))
    order = saved.get('order', '')
    options = {'order': order if order in tuple(value for _, value in ORDER) else ''}
    for field in FILTERS:
        selected = saved.get(field.key, ())
        if isinstance(selected, str):
            selected = (selected,)
        if not isinstance(selected, (list, tuple)):
            selected = ()
        options[field.key] = tuple(value for _, value in field.choices if value in selected)
    return options


def link_options(config):
    saved = _mapping(config.get('link'))
    return {
        'url_extension': saved.get('url_extension', True) is not False,
        'content_type': saved.get('content_type', False) is True,
    }


def cache_seconds(config):
    try:
        value = int(config.get('cache_seconds', DEFAULT_CACHE_SECONDS))
    except (TypeError, ValueError, OverflowError):
        value = DEFAULT_CACHE_SECONDS
    return max(0, min(MAX_CACHE_SECONDS, value))


def save_preferences(config, *, mirrors, search, link, open_external, prefer_epub, cache_seconds, tags):
    # JSONConfig commits once when exiting its context manager.
    context = config if hasattr(config, '__enter__') else nullcontext()
    values = {
        'mirrors': list(mirrors),
        'search': {**_mapping(config.get('search')), **search},
        'link': {**_mapping(config.get('link')), **link},
        'open_external': open_external,
        'prefer_epub': prefer_epub,
        'cache_seconds': cache_seconds,
        'tags': tags,
    }
    with context:
        config.update(values)
