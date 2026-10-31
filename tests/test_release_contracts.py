"""No-GPU release, quality-metric and durable partial-result contracts."""
from __future__ import annotations
import importlib.util
import json
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from localocr.checkpoints import save_header, save_page, read_partial
from quality_metrics import character_error_rate, table_cells

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('localocr_release_test_target', ROOT / 'scripts/manage_runtime.py')
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class ReleaseContractTests(unittest.TestCase):
    def runtime_fixture(self, root, name):
        target = root / name
        (target / 'app').mkdir(parents=True); (target / 'acceptance').mkdir()
        (target / 'pyvenv.cfg').write_text('home = /usr/bin')
        (target / 'app/snapshot.json').write_text(json.dumps({'schema': 'localocr.source-snapshot.v1', 'files': {'localocr/__init__.py': 'fixture'}}))
        (target / 'acceptance/acceptance.json').write_text(json.dumps({'schema': 'localocr.runtime-acceptance.v1', 'ok': True, 'python_prefix': str(target.resolve()), 'source_root': str(target.resolve() / 'app')}))
        return target

    def test_retirement_preserves_protected_and_unpublished_and_unlinks_shared_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active, previous, candidate, first, second = [self.runtime_fixture(root, n) for n in ('active', 'retained', 'candidate', 'old-a', 'old-b')]
            (root / 'current').symlink_to(active); (root / 'previous').symlink_to(previous)
            for protected in (active, previous):
                with self.assertRaisesRegex(RuntimeError, 'Cannot enroll'):
                    manager.enroll_retirements(root, [protected.name])
            (first / 'shared').write_bytes(b'one physical payload'); os.link(first / 'shared', second / 'shared')
            self.assertEqual((first / 'shared').stat().st_ino, (second / 'shared').stat().st_ino)
            remove = shutil.rmtree; remaining_links = []
            def removing(path):
                remove(path)
                if (second / 'shared').exists(): remaining_links.append((second / 'shared').stat().st_nlink)
            with patch.object(manager, 'ensure_retirement_idle'), patch.object(manager.shutil, 'rmtree', side_effect=removing):
                result = manager.retire(root, [first.name, second.name])
            self.assertEqual(result['deleted'], [first.name, second.name]); self.assertEqual(remaining_links, [1])
            self.assertEqual(result['pending'], {})
            self.assertTrue(all(path.exists() for path in (active, previous, candidate)))

    def test_partial_retirement_failure_is_retryable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); target = self.runtime_fixture(root, 'old')
            def partial_failure(path):
                (path / 'pyvenv.cfg').unlink(); raise OSError('fixture busy')
            with patch.object(manager, 'ensure_retirement_idle'), patch.object(manager.shutil, 'rmtree', side_effect=partial_failure):
                result = manager.retire(root, ['old'])
            self.assertIn('fixture busy', result['pending']['old']['reason'])
            with patch.object(manager, 'ensure_retirement_idle'):
                self.assertEqual(manager.retire(root)['deleted'], ['old'])
            self.assertFalse(target.exists())

    def test_retirement_rejects_links_escape_and_replaced_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); target = self.runtime_fixture(root, 'old')
            (root / 'alias').symlink_to(target)
            for name in ('alias', '../outside'):
                with self.assertRaises(RuntimeError): manager.enroll_retirements(root, [name])
            manager.enroll_retirements(root, ['old'])
            target.rename(root / 'original'); self.runtime_fixture(root, 'old')
            with patch.object(manager, 'ensure_retirement_idle') as idle:
                result = manager.retire(root)
            self.assertIn('identity changed', result['pending']['old']['reason']); idle.assert_not_called()
            self.assertTrue((root / 'old').is_dir())

    def test_busy_retirement_and_changed_protection_are_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); target = self.runtime_fixture(root, 'old')
            with patch.object(manager, 'ensure_retirement_idle', side_effect=RuntimeError('process busy')):
                result = manager.retire(root, ['old'])
            self.assertIn('process busy', result['pending']['old']['reason'])
            (root / 'previous').symlink_to(target)
            with patch.object(manager, 'ensure_retirement_idle') as idle:
                result = manager.retire(root)
            self.assertIn('protected', result['pending']['old']['reason']); idle.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'Linux retirement process inspection')
    def test_retirement_detects_a_real_process_using_the_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            process = manager.subprocess.Popen([manager.sys.executable, '-c', 'import time; time.sleep(30)'], cwd=target)
            try:
                with patch.object(manager, 'ensure_stopped'):
                    with self.assertRaisesRegex(RuntimeError, 'in use by process'):
                        manager.ensure_retirement_idle(target)
            finally:
                process.terminate(); process.wait(timeout=5)

    def test_successful_switch_survives_cleanup_failure_and_rollback_target_is_protected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new = [self.runtime_fixture(root, n) for n in ('old', 'new')]
            (root / 'current').symlink_to(old); (root / 'previous').symlink_to(new)
            receipt = root / 'validation.json'
            receipt.write_text(json.dumps({'ok': True, 'python_prefix': str(new), 'state': {}}))
            output = io.StringIO()
            with patch.object(manager.sys, 'prefix', str(new)), patch.object(manager, 'ROOT', new / 'app'), patch.object(manager, 'state', return_value={}), patch.object(manager, 'ensure_stopped'), patch.object(manager, 'retire', side_effect=OSError('cleanup failed')), patch('sys.stdout', output):
                manager.activate(root, receipt)
            self.assertEqual((root / 'current').resolve(), new); self.assertEqual((root / 'previous').resolve(), old)
            self.assertIn('retirement_error', json.loads(output.getvalue()))
            self.assertEqual(manager.load_retirements(root), {})

    def test_rollback_timeout_reports_success_only_after_both_links_switched(self):
        for switched in (False, True):
            with self.subTest(switched=switched), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); old = root / 'old'; target = root / 'target'; old.mkdir(); target.mkdir()
                current = root / 'current'; previous = root / 'previous'
                current.symlink_to(old); previous.symlink_to(target)
                def timeout(*args, **kwargs):
                    if switched:
                        current.unlink(); previous.unlink(); current.symlink_to(target); previous.symlink_to(old)
                    raise manager.subprocess.TimeoutExpired('activate', 120)
                output = io.StringIO()
                with patch.object(manager.subprocess, 'run', side_effect=timeout), patch('sys.stdout', output):
                    if switched: manager.rollback(root)
                    else:
                        with self.assertRaises(manager.subprocess.TimeoutExpired): manager.rollback(root)
                if switched:
                    result = json.loads(output.getvalue()); self.assertEqual(result['active'], str(target))
                    self.assertIn('timed out', result['retirement_error'])

    def test_metrics_ignore_whitespace_not_wrong_digits(self):
        self.assertEqual(character_error_rate('金额 1280.50', '金额\n1280.50'), 0)
        self.assertGreater(character_error_rate('金额 1280.50', '金额1280.60'), 0)
        self.assertEqual(character_error_rate('', 'hallucination'), 1)

    def test_table_cells_preserve_order(self):
        data = {'pages': [{'blocks': [{'text': '<table><tr><td>A</td><td>2</td></tr><tr><td>B</td><td>3</td></tr></table>'}]}]}
        self.assertEqual(table_cells(data), ['A', '2', 'B', '3'])

    def test_checkpoint_requires_same_source_and_model_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {'profile_id': 'ocr', 'profile_revision': 'v1', 'source_sha256': 'hash', 'path': '/snapshot/a.png'}
            save_header(root, {'expected_page_count': 2}, payload)
            save_page(root, {'page_index': 0, 'blocks': [{'text': 'first'}]})
            self.assertEqual(len(read_partial(root, payload)['pages']), 1)
            self.assertIsNone(read_partial(root, {**payload, 'profile_revision': 'v2'}))
            (root / 'page_000001.json').write_text('{broken')
            self.assertIsNone(read_partial(root, payload))

    def test_activation_fails_for_stale_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); receipt = root / 'acceptance.json'
            receipt.write_text(json.dumps({'ok': True, 'python_prefix': str(Path(manager.sys.prefix).resolve()), 'state': {'version': 1}}))
            with patch.object(manager, 'state', return_value={'version': 2}), patch.object(manager, 'ensure_stopped') as stopped:
                with self.assertRaisesRegex(RuntimeError, 'stale'):
                    manager.activate(root, receipt)
                stopped.assert_not_called()
            self.assertFalse((root / 'current').exists())

    def test_atomic_activation_preserves_previous_and_switches_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); old = root / 'old'; new = root / 'new'; old.mkdir(); new.mkdir()
            older = self.runtime_fixture(root, 'older'); (root / 'previous').symlink_to(older)
            (root / 'current').symlink_to(old)
            receipt = root / 'acceptance.json'
            receipt.write_text(json.dumps({'ok': True, 'python_prefix': str(new), 'state': {'version': 1}}))
            with patch.object(manager.sys, 'prefix', str(new)), patch.object(manager, 'ROOT', new / 'app'), patch.object(manager, 'state', return_value={'version': 1}), patch.object(manager, 'ensure_stopped'), patch.object(manager, 'ensure_retirement_idle'):
                manager.activate(root, receipt)
            self.assertEqual((root / 'current').resolve(), new)
            self.assertEqual((root / 'previous').resolve(), old)
            self.assertFalse(older.exists())

    def test_existing_directory_is_not_overwritten_by_activation_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); current = root / 'current'; current.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'non-symlink'):
                manager.atomic_link(current, root / 'target')
            self.assertTrue(current.is_dir())

    def test_source_snapshot_excludes_results_and_does_not_follow_future_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'source'; destination = root / 'release/app'
            (source / 'localocr').mkdir(parents=True)
            code = source / 'localocr/__init__.py'; code.write_text('VERSION = 1')
            (source / 'outputs').mkdir(); (source / 'outputs/private.json').write_text('not for release')
            result = manager.freeze_source(source, destination)
            code.write_text('VERSION = 2')
            self.assertEqual((destination / 'localocr/__init__.py').read_text(), 'VERSION = 1')
            self.assertFalse((destination / 'outputs').exists())
            self.assertTrue((destination / 'snapshot.json').is_file())
            self.assertEqual(result['files'], 1)
            with self.assertRaisesRegex(RuntimeError, 'already exists'):
                manager.freeze_source(source, destination)

    def test_validation_requires_explicit_heavy_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'allow-heavy'):
                manager.validate(Path(temporary), False)

if __name__ == '__main__':
    unittest.main()
