#!/usr/bin/env python3
"""Bounded release validation and atomic interpreter selection, without a new daemon."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def freeze_source(source: Path, destination: Path) -> dict:
    """Copy only the release's known code/test inputs, never outputs, jobs or .git."""
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("Release source already exists; create a new candidate instead of overwriting")
    files = [source / "pyproject.toml", source / "README.md", source / "LICENSE", source / "AGENTS.md"] + list(source.glob("*.ps1")) + list(source.glob("*.bat")) + list((source / "docs").glob("*.md"))
    for directory, patterns in [("localocr", ("*.py", "*.json")), ("scripts", ("*.py", "*.sh", "*.ps1")),
                                ("tests", ("*.py",)), ("requirements", ("*.txt",))]:
        for pattern in patterns:
            files.extend((source / directory).rglob(pattern))
    # These files are generated public fixtures already owned by the project.
    for name in ("probe_text.png", "sample_chat_screenshot.png", "sample_formula.png", "sample_scan.pdf", "sample_table.png"):
        fixture = source / "tests/samples" / name
        if fixture.is_file(): files.append(fixture)
    temporary = destination.with_name(destination.name + f".preparing-{os.getpid()}")
    temporary.mkdir(parents=True, exist_ok=False)
    copied = {}
    try:
        for path in sorted(set(files)):
            relative = path.relative_to(source)
            if "__pycache__" in relative.parts or not path.is_file(): continue
            if path.is_symlink(): raise RuntimeError(f"Release source must be a regular file: {relative}")
            content = path.read_bytes()
            target = temporary / relative; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content); target.chmod(path.stat().st_mode & 0o777)
            digest = hashlib.sha256(content).hexdigest()
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError("Source changed while creating a release snapshot")
            copied[str(relative)] = digest
        if not (temporary / "localocr/__init__.py").is_file():
            raise RuntimeError("No LocalOCR package found in the source snapshot")
        if any(hashlib.sha256((source / relative).read_bytes()).hexdigest() != digest for relative, digest in copied.items()):
            raise RuntimeError("Source changed before the release snapshot was committed")
        (temporary / "snapshot.json").write_text(json.dumps({"schema": "localocr.source-snapshot.v1", "files": copied}, indent=2), encoding="utf-8")
        os.rename(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary)  # Only this invocation's not-yet-published staging directory.
        raise
    return {"source_snapshot": str(destination), "files": len(copied)}


def state() -> dict:
    from localocr.model_registry import load_model_profiles
    from localocr.release_identity import execution_sha256
    files = [ROOT / 'pyproject.toml', ROOT / 'README.md', ROOT / 'LICENSE', ROOT / 'AGENTS.md'] + list(ROOT.glob('*.ps1')) + list(ROOT.glob('*.bat')) + list((ROOT / 'docs').glob('*.md'))
    for relative, patterns in [('localocr', ('*.py', '*.json')), ('scripts', ('*.py', '*.sh', '*.ps1')), ('tests', ('*.py',)), ('requirements', ('*.txt',))]:
        for pattern in patterns:
            files.extend((ROOT / relative).rglob(pattern))
    content = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(files)) if p.is_file() and '__pycache__' not in p.parts}
    packages = {d.metadata['Name'].lower().replace('_', '-'): d.version for d in importlib.metadata.distributions() if d.metadata.get('Name')}
    models = {p.id: execution_sha256(p) for p in load_model_profiles().profiles.values()}
    return {'source_sha256': hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest(), 'packages': packages, 'models': models, 'python': list(sys.version_info[:2])}


def ensure_stopped() -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open('http://127.0.0.1:18665/health', timeout=3)
    except urllib.error.HTTPError as exc:
        raise RuntimeError('A service is listening on 18665; inspect its owner before switching') from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError): return
        raise RuntimeError('Cannot establish that the local service is stopped') from exc
    else:
        response.close()
        raise RuntimeError('Stop the existing LocalOCR coordinator with stop_server.ps1 before switching')


def atomic_link(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_symlink():
        raise RuntimeError(f'Refusing to replace a non-symlink path: {path}')
    temporary = path.with_name('.' + path.name + f'.{os.getpid()}')
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate(directory: Path, allow_heavy: bool) -> int:
    if not allow_heavy: raise ValueError('validate requires --allow-heavy')
    directory.mkdir(parents=True, exist_ok=True)
    before = state(); started = time.time()
    report = {'source_root': str(ROOT), 'schema': 'localocr.runtime-acceptance.v1', 'python_prefix': str(Path(sys.prefix).resolve()), 'started_unix': started, 'ok': False}
    commands = [
        ('dependencies', [sys.executable, '-m', 'pip', 'check'], 60),
        ('unit', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py', '-q'], 300),
        ('quality', [sys.executable, '-B', 'tests/run_tests.py', '--allow-heavy', '--out-dir', str(directory)], 1800),
    ]
    for name, command, timeout in commands:
        print(f'Validating {name}', flush=True)
        with (directory / f'{name}.log').open('w', encoding='utf-8') as log:
            try:
                completed = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
                report[name + '_exit_code'] = completed.returncode
            except subprocess.TimeoutExpired:
                report[name + '_exit_code'] = 124
        if report[name + '_exit_code'] != 0:
            report['failed_stage'] = name
            break
    after = state(); report['state'] = after; report['stable_during_validation'] = before == after
    report['ok'] = report['stable_during_validation'] and all(report.get(name + '_exit_code') == 0 for name in ['dependencies', 'unit', 'quality'])
    report['elapsed_sec'] = time.time() - started
    (directory / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'ok': report['ok'], 'failed_stage': report.get('failed_stage'), 'receipt': str(directory / 'acceptance.json')}, ensure_ascii=False), flush=True)
    return 0 if report['ok'] else 1


def activate(runtime_root: Path, receipt: Path) -> None:
    record = json.loads(receipt.read_text(encoding='utf-8'))
    target = Path(sys.prefix).resolve()
    if record.get('ok') is not True or record.get('python_prefix') != str(target) or record.get('state') != state():
        raise RuntimeError('Candidate acceptance is missing, failed or stale; rerun validate')
    if ROOT != target / "app":
        raise RuntimeError("Activate from the candidate source snapshot, not the editable development checkout")
    ensure_stopped()
    current, previous = runtime_root / 'current', runtime_root / 'previous'
    old = current.resolve(strict=True) if current.is_symlink() else (Path('/root/localocr-venv') if Path('/root/localocr-venv/bin/python').is_file() else None)
    if old is not None and old != target: atomic_link(previous, old)
    atomic_link(current, target)
    print(json.dumps({'active': str(current.resolve(strict=True)), 'previous': str(previous.resolve()) if previous.is_symlink() else None}))


def rollback(runtime_root: Path) -> None:
    previous = runtime_root / 'previous'
    if not previous.is_symlink(): raise RuntimeError('No previous runtime is registered')
    target = previous.resolve(strict=True)
    # The previous runtime validates its own interpreter, packages, weights and code.
    subprocess.run([str(target / 'bin/python'), '-P', str(target / 'app/scripts/manage_runtime.py'), 'activate', '--runtime-root', str(runtime_root), '--receipt', str(target / 'acceptance/acceptance.json')], check=True, cwd=target / 'app', timeout=120)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['freeze', 'inspect', 'validate', 'activate', 'rollback'])
    parser.add_argument('--runtime-root', type=Path, default=Path('/root/localocr-runtimes'))
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--report-dir', type=Path)
    parser.add_argument('--allow-heavy', action='store_true')
    args = parser.parse_args(argv)
    if args.action == 'freeze':
        print(json.dumps(freeze_source(ROOT, Path(sys.prefix).resolve() / 'app'))); return 0
    if args.action == 'inspect':
        print(json.dumps({'python_prefix': sys.prefix, 'source_root': str(ROOT), 'state': state()}, ensure_ascii=False)); return 0
    if args.action == 'validate': return validate(args.report_dir or Path(sys.prefix) / 'acceptance', args.allow_heavy)
    if args.action == 'activate': activate(args.runtime_root, args.receipt or Path(sys.prefix) / 'acceptance/acceptance.json')
    if args.action == 'rollback': rollback(args.runtime_root)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
