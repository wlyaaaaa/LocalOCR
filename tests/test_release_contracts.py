"""No-GPU release, quality-metric and durable partial-result contracts."""
from __future__ import annotations
import importlib.util
import json
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
            (root / 'current').symlink_to(old)
            receipt = root / 'acceptance.json'
            receipt.write_text(json.dumps({'ok': True, 'python_prefix': str(new), 'state': {'version': 1}}))
            with patch.object(manager.sys, 'prefix', str(new)), patch.object(manager, 'ROOT', new / 'app'), patch.object(manager, 'state', return_value={'version': 1}), patch.object(manager, 'ensure_stopped'):
                manager.activate(root, receipt)
            self.assertEqual((root / 'current').resolve(), new)
            self.assertEqual((root / 'previous').resolve(), old)

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