"""Build a verified source archive, optionally with the three Windows launchers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {'.git', '.agents', '.codex', '.aws', '.venv', '.deps', 'node_modules',
                 'var', 'dist', 'build', '__pycache__', '.pytest_cache', 'test-results', 'playwright-report'}
SOURCE_DIRS = {'backend', 'frontend', 'scripts', 'docs', 'deploy', 'datasets', '.github', 'evals'}
ROOT_FILES = {'README.md', '.env.example', '.gitignore', '.dockerignore', 'compose.yaml',
              '.gitattributes', 'Dockerfile.backend', 'Dockerfile.frontend', 'Dockerfile.cloud', 'render.yaml'}
LAUNCHERS = {'FinAgent.exe', 'FinAgent-Mock.exe', 'FinAgent-Live.exe'}
SUFFIXES = {'.py', '.toml', '.ini', '.lock', '.txt', '.md', '.json', '.ts', '.tsx', '.js',
            '.cjs', '.mjs', '.css', '.html', '.svg', '.ico', '.png', '.ps1', '.cs', '.conf', '.yaml', '.yml', '.xml', '.csv', '.jsonl', '.sh'}
REQUIRED = {'backend/tests/test_security.py', 'backend/tests/test_background.py',
            'backend/tests/test_migrations.py', 'backend/migrations/versions/0013_security.py',
            'backend/requirements.lock', 'frontend/package-lock.json',
            'frontend/tests/api-regression.cjs', 'frontend/tests/p0-browser.mjs',
            'docs/P0-验收记录.md', 'scripts/test-p0.ps1', '.env.example', 'README.md',
            'scripts/Launcher.cs', 'scripts/ServiceProcesses.cs', 'scripts/build-launcher.ps1',
            'scripts/launcher-run.ps1', 'scripts/dev.ps1', 'scripts/setup.ps1',
            'scripts/install-financial-demo.py', 'scripts/import-financial-dataset.py',
            'scripts/initialize-demo.py',
            'datasets/demo_financial_v2/csv_file_manifest.json', 'datasets/demo_financial_v2/manifest.json'}


def accepted(path, *, include_launchers=False):
    rel = path.relative_to(ROOT)
    if path.is_symlink() or any(part in EXCLUDED_DIRS or part.startswith('.venv') or part.endswith('.egg-info') for part in rel.parts):
        return False
    if path.name.startswith('.env') and path.name != '.env.example':
        return False
    if len(rel.parts) == 1:
        return path.name in ROOT_FILES or include_launchers and path.name in LAUNCHERS
    return rel.parts[0] in SOURCE_DIRS and path.suffix in SUFFIXES


def source_paths(*, include_launchers=False):
    # Prune runtime directories before walking, including inaccessible test ACL
    # directories. Never enumerate dependencies, private inboxes or databases.
    for directory, children, filenames in os.walk(ROOT, followlinks=False):
        base = Path(directory)
        children[:] = [name for name in children if name not in EXCLUDED_DIRS
            and not name.startswith('.venv') and not name.endswith('.egg-info')
            and not (base / name).is_symlink() and (base != ROOT or name in SOURCE_DIRS)]
        for filename in filenames:
            path = base / filename
            if accepted(path, include_launchers=include_launchers):
                yield path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', default='FinAgent-source-candidate.zip')
    parser.add_argument('--include-launchers', action='store_true',
                        help='Include the three root Windows launchers; build them first if missing')
    args = parser.parse_args()
    if Path(args.name).name != args.name or not args.name.endswith('.zip'):
        raise SystemExit('Archive name must be a .zip filename, without directories')
    paths = sorted(source_paths(include_launchers=args.include_launchers))
    names = {path.relative_to(ROOT).as_posix() for path in paths}
    required = REQUIRED | (LAUNCHERS if args.include_launchers else set())
    if required - names:
        raise SystemExit('Required files missing: ' + str(sorted(required - names)))
    manifest = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    directory = ROOT / 'var' / 'artifacts'
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / args.name
    if archive.exists():
        raise SystemExit('Refusing to overwrite an existing archive; use --name with a new filename')
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as out:
        for path in paths:
            out.write(path, 'finagent/' + path.relative_to(ROOT).as_posix())
        out.writestr('SOURCE-MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(archive) as check:
        if check.testzip() is not None:
            raise SystemExit('ZIP integrity check failed')
        for name, expected in manifest.items():
            if hashlib.sha256(check.read('finagent/' + name)).hexdigest() != expected:
                raise SystemExit('Archive digest mismatch: ' + name)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(checksum + '  ' + archive.name + '\n', encoding='ascii')
    print(json.dumps({'archive': str(archive), 'source_files': len(paths), 'sha256': checksum,
                      'windows_launchers_included': args.include_launchers,
                      'verified': True, 'release_status': 'candidate-not-integration-accepted'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
