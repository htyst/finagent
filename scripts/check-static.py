"""Dependency-free syntax, local import and packaged test-entry checks."""
import ast
import json
from pathlib import Path
import shlex

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
errors = []
parsed = {}
for path in sorted(BACKEND.rglob('*.py')):
    try:
        parsed[path] = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    except SyntaxError as error:
        errors.append(f'{path.relative_to(ROOT)}:{error.lineno}: {error.msg}')


def symbols(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                    names.add(child.id)
    return names


for path, tree in parsed.items():
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module or not node.module.startswith('app.'):
            continue
        base = BACKEND.joinpath(*node.module.split('.'))
        target = base.with_suffix('.py') if base.with_suffix('.py').exists() else base / '__init__.py'
        if not target.exists():
            errors.append(f'{path.relative_to(ROOT)}:{node.lineno}: missing module {node.module}')
            continue
        exported = symbols(parsed[target]) if target in parsed else set()
        for alias in node.names:
            if alias.name != '*' and alias.name not in exported and not (base / (alias.name + '.py')).exists():
                errors.append(f'{path.relative_to(ROOT)}:{node.lineno}: missing {node.module}.{alias.name}')

package = json.loads((ROOT / 'frontend' / 'package.json').read_text())
for name, command in package['scripts'].items():
    for part in shlex.split(command):
        if part.startswith('tests/') and not (ROOT / 'frontend' / part).is_file():
            errors.append(f'package.json {name}: missing {part}')
result = {'check': 'static-only', 'python_files': len(parsed), 'test_scripts': len(package['scripts']), 'errors': errors}
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(bool(errors))
