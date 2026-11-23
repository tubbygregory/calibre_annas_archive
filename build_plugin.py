"""Build the Calibre installation ZIP using only Python's standard library."""

import argparse
import ast
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parent
FILES = (
    '__init__.py',
    'annas_archive.py',
    'config.py',
    'constants.py',
    'downloads.py',
    'http_client.py',
    'mirrors.py',
    'parsing.py',
    'plugin-import-name-store_annas_archive.txt',
    'search.py',
    'settings.py',
)


def plugin_version():
    module = ast.parse((ROOT / '__init__.py').read_text(encoding='utf-8'))
    plugin = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == 'AnnasArchiveStore')
    for statement in plugin.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == 'version' for target in statement.targets
        ):
            return '.'.join(map(str, ast.literal_eval(statement.value)))
    raise ValueError('Plugin metadata must contain a version tuple.')


def build(output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f'calibre_annas_archive-v{plugin_version()}.zip'
    with ZipFile(destination, 'w') as archive:
        for name in FILES:
            entry = ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, (ROOT / name).read_bytes(), compress_type=ZIP_DEFLATED, compresslevel=9)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    print(build(args.output_dir).resolve())


if __name__ == '__main__':
    main()
