"""Regenerate the integration file-purpose inventory from its pinned Git input."""
import ast
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[4]
BASE = 'f6c4aaa3f568759472d8ccc1a3420bbd0cd37e35'
OUTPUT = Path(__file__).with_name('change-inventory.json')


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True).stdout.decode('utf-8').splitlines()


def purpose(path):
    if '/evidence/' in path and path.endswith('.txt.json'):
        return 'Exact command arguments, working directory and exit status for the adjacent unedited output.'
    if '/evidence/' in path and path.endswith('.txt'):
        return 'Unedited verification or incident command output; retained as dated evidence.'
    target = ROOT/path
    if not target.exists():
        return 'Original discovery schema placement, superseded by migration 006 after P2 storage initialization.'
    if target.suffix == '.py':
        value = ast.get_docstring(ast.parse(target.read_text(encoding='utf-8')))
        return value.split('\n')[0] if value else 'Executable integration/verification support; inspected in the integration diff.'
    if target.suffix == '.md':
        return next((line.lstrip('# ') for line in target.read_text(encoding='utf-8').splitlines() if line.startswith('# ')), 'Phase documentation and acceptance evidence.')
    roles = {'.sql': 'Idempotent schema contract or exact historical upgrade fixture.',
             '.html': 'Server-rendered chart, methodology or operational status combining P2 and discovery.',
             '.sh': 'Pinned PostgreSQL client executable and library staging for native backup/restore.',
             '.json': 'Structured verification evidence or its reproducible inventory.'}
    if target.name == 'Dockerfile':
        return 'Pinned application image including native PostgreSQL backup/restore clients.'
    return roles.get(target.suffix, 'Retained integration input or verification artifact.')


paths = sorted(set(git('diff', '--no-renames', '--name-only', BASE)) | set(git('ls-files', '--others', '--exclude-standard')) | {OUTPUT.relative_to(ROOT).as_posix()})
base_paths = set(git('ls-tree', '-r', '--name-only', BASE))
rows = []
for path in paths:
    action = 'DELETE' if not (ROOT/path).exists() and path != OUTPUT.relative_to(ROOT).as_posix() else 'MODIFY' if path in base_paths else 'NEW'
    reason = ('Preserve prior P2 implementation and acceptance evidence while integrating the combined product.'
              if '/p2-storage/' in path or '/p2-reliable-collection/' in path else
              'Join P2 scheduling/storage/recovery with catalog/profile main, or provide its regression and handoff evidence.')
    rows.append({'path': path, 'action': action, 'purpose': purpose(path), 'reason': reason})
OUTPUT.write_text(json.dumps({'base': BASE, 'files': rows}, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
print(json.dumps({'base': BASE, 'files_described': len(rows), 'inventory': str(OUTPUT)}, indent=2))
