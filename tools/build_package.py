"""Build and verify the byte-preserving release using only the standard library."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_file(relative):
    parts = PurePosixPath(relative).parts
    if not parts or '\\' in relative or ':' in relative or '..' in parts or relative.startswith('/'):
        raise ValueError(f'잘못된 상대 경로입니다: {relative}')
    path = ROOT
    for part in parts:
        path = path / part
        if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f'연결된 폴더와 파일은 설치하지 않습니다: {relative}')
    if not path.is_file() or not path.resolve().is_relative_to(ROOT):
        raise ValueError(f'묶음에 없는 파일입니다: {relative}')
    return path


def expected_manifest():
    config = json.loads((ROOT / 'tools/package-config.json').read_text(encoding='utf-8'))
    if config['closing_line'] not in (ROOT / 'INSTALL.md').read_text(encoding='utf-8-sig').splitlines():
        raise ValueError('INSTALL.md에 closing_line과 같은 줄이 없습니다.')
    names = list(config['root_files'])
    for skill in config['skills']:
        safe_file(f'{skill}/SKILL.md')
        for directory, dirs, files in os.walk(ROOT / skill, followlinks=False):
            for name in dirs + files:
                node = Path(directory) / name
                if node.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError(f'연결된 폴더와 파일은 설치하지 않습니다: {node}')
            dirs[:] = [d for d in dirs if d != '__pycache__']
            names.extend((Path(directory) / name).relative_to(ROOT).as_posix()
                         for name in files if not name.lower().endswith('.pyc'))
    if len(set(names)) != len(names):
        raise ValueError('같은 경로가 두 번 들어 있습니다.')
    manifest = {key: config[key] for key in ('package', 'version', 'skills', 'closing_line')}
    manifest['files'] = [{'path': name, 'sha256': digest(safe_file(name))} for name in sorted(names)]
    return config, manifest


def build(config, manifest):
    manifest_path = ROOT / 'MANIFEST.json'
    manifest_path.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').replace('\n', '\r\n').encode('utf-8'))
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    if dist.is_symlink() or dist.resolve() != ROOT / 'dist':
        raise ValueError('dist 폴더 경로가 올바르지 않습니다.')
    archive = dist / config['zip_name']
    if archive.parent != dist or archive.name != config['zip_name']:
        raise ValueError('zip_name이 올바르지 않습니다.')
    if archive.exists():
        archive.unlink()
    names = sorted(['MANIFEST.json'] + [entry['path'] for entry in manifest['files']])
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 21, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(info, (ROOT / name).read_bytes())
    # Keep transient extraction inside the allowed project root; remove it on exit.
    with tempfile.TemporaryDirectory(prefix='.verify-', dir=dist) as temporary:
        with zipfile.ZipFile(archive) as source:
            if source.namelist() != names:
                raise ValueError('ZIP 항목이 MANIFEST와 다릅니다.')
            source.extractall(temporary)
        extracted = Path(temporary)
        expected = {entry['path']: entry['sha256'] for entry in manifest['files']}
        expected['MANIFEST.json'] = digest(manifest_path)
        actual = {p.relative_to(extracted).as_posix(): digest(p) for p in extracted.rglob('*') if p.is_file()}
        if actual != expected:
            raise ValueError('ZIP 안 파일의 해시가 MANIFEST와 다릅니다.')
    return {'zip': str(archive), 'sha256': digest(archive), 'bytes': archive.stat().st_size,
            'entries': len(names), 'version': config['version']}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('build', 'check'):
        raise ValueError('사용법: python -B tools/build_package.py build | check')
    config, manifest = expected_manifest()
    if sys.argv[1] == 'build':
        return build(config, manifest)
    current = json.loads((ROOT / 'MANIFEST.json').read_text(encoding='utf-8'))
    if current != manifest:
        raise ValueError('MANIFEST.json이 현재 파일과 다릅니다. build를 다시 실행하세요.')
    return {'status': 'PASS', 'version': config['version']}


if __name__ == '__main__':
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'status': 'FAIL', 'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
