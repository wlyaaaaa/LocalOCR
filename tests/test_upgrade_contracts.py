"""Regression cases for adapter correctness and model/runtime replacement."""
from __future__ import annotations
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from localocr.engines.common import recognized_lines, combine_predictions, optional_score
from localocr.model_registry import load_model_profiles, resolve_model_reference
from localocr.pdf_utils import input_page_count, iter_input_pages
from localocr.objective_result import annotate_result
from localocr.difficulty import assess_ocr_pages
from localocr.release_identity import execution_sha256
from localocr.runtime import ExecutionError, _predict
from localocr.service import _request_variant, _merge_escalated_pages


class UpgradeContractsTest(unittest.TestCase):
    def test_filtered_text_uses_recognition_geometry(self):
        a = [[0, 0], [10, 0], [10, 10], [0, 10]]
        b = [[100, 100], [110, 100], [110, 110], [100, 110]]
        row = recognized_lines({'dt_polys': [a, b], 'rec_polys': [b], 'rec_texts': ['B'], 'rec_scores': [.99]})[0]
        self.assertEqual(row['text'], 'B')
        self.assertEqual(row['polygon'], b)
        self.assertEqual(row['rect'], [100, 100, 110, 110])

    def test_unfiltered_detections_are_not_fabricated_into_recognition(self):
        row = recognized_lines({'dt_polys': [[1], [2]], 'rec_texts': ['B']})[0]
        self.assertIsNone(row['polygon'])

    def test_inconsistent_present_scores_fail(self):
        with self.assertRaisesRegex(ValueError, 'alignment'):
            recognized_lines({'rec_texts': ['A'], 'rec_scores': [.1, .2]})

    def test_scores_reject_nan_infinity_and_bool(self):
        for value in [float('nan'), float('inf'), True, -1, 2]:
            self.assertIsNone(optional_score(value))

    def test_generator_result_consumes_all_pages_and_marks_transform(self):
        outputs = ({'res': {'doc_preprocessor_res': {'angle': 90}}} for _ in range(2))
        result = combine_predictions(outputs, lambda _: {'pages': [{'blocks': []}]}, options={'use_doc_unwarping': True})
        self.assertEqual([p['page_index'] for p in result['pages']], [0, 1])
        self.assertEqual(result['pages'][1]['coordinate_reference']['original_mapping_status'], 'unavailable_non_linear_transform')

    def test_actual_tiff_frame_count_overrides_false_one_page_declaration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'two.tiff'
            first, second = Image.new('RGB', (80, 60), 'white'), Image.new('RGB', (80, 60), 'black')
            first.save(path, save_all=True, append_images=[second])
            self.assertEqual(input_page_count(path), 2)
            result = annotate_result({'expected_page_count': 1, 'pages': [{'page_index': 0, 'blocks': []}]}, path,
                                     processor='test', model_id='test', pipeline_version='test')
            self.assertEqual(result['objective_result']['coverage']['pages_expected'], 2)
            self.assertNotEqual(result['objective_result']['coverage']['status'], 'complete')
            self.assertNotEqual(result['objective_outcome'], 'no_text_detected')

    def test_streamed_frames_removed_when_advanced_and_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); path = root / 'two.tiff'
            a, b = Image.new('RGB', (80, 60), 'white'), Image.new('RGB', (80, 60), 'black')
            a.save(path, save_all=True, append_images=[b])
            stream = iter_input_pages(path, root / 'pages')
            i, first, _ = next(stream)
            self.assertTrue(first.exists()); self.assertEqual(i, 0)
            j, second, _ = next(stream)
            self.assertFalse(first.exists()); self.assertEqual(j, 1)
            stream.close(); self.assertFalse(second.exists())

    def test_pdf_subset_preserves_original_indices_and_bounded_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); path = root / 'two.pdf'
            a, b = Image.new('RGB', (240, 160), 'white'), Image.new('RGB', (240, 160), 'black')
            a.save(path, save_all=True, append_images=[b], resolution=144)
            pages = iter_input_pages(path, root / 'pages', indices=[1], dpi=216, max_pixels=100_000)
            index, rendered, meta = next(pages)
            self.assertEqual(index, 1); self.assertLessEqual(meta['rendered_width'] * meta['rendered_height'], 102000)
            self.assertEqual(meta['render_dpi'], 216)
            pages.close(); self.assertFalse(rendered.exists())

    def test_difficult_page_not_hidden_by_many_good_pages(self):
        good = {'blocks': [{'text': 'good', 'score': .99}]}
        pages = [{**good, 'page_index': i} for i in range(20)] + [{'page_index': 20, 'blocks': [{'text': 'bad', 'score': .1}]}]
        self.assertEqual([p['page_index'] for p in assess_ocr_pages({'pages': pages}) if p['should_escalate']], [20])

    def test_subset_merge_preserves_good_page_and_first_pass(self):
        a, b = resolve_model_reference('ocr'), resolve_model_reference('vl')
        first = {'pages': [{'page_index': 0, 'blocks': [{'text': 'good'}]}, {'page_index': 1, 'blocks': [{'text': 'uncertain'}]}]}
        second = {'pages': [{'page_index': 1, 'blocks': [{'text': 'second'}]}]}
        result = _merge_escalated_pages(first, second, [1], a, b)
        self.assertEqual(result['pages'][0]['blocks'][0]['text'], 'good')
        self.assertEqual(result['pages'][1]['first_pass_evidence']['blocks'][0]['text'], 'uncertain')
        self.assertTrue(result['mixed_page_models'])
        with self.assertRaises(ExecutionError):
            _merge_escalated_pages(first, second, [0], a, b)

    def test_registry_refresh_and_default_vl_change_invalidate_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            from localocr.model_registry import DEFAULT_PROFILE_PATH
            raw = json.loads(DEFAULT_PROFILE_PATH.read_text())
            for p in raw['profiles']: p['artifact_paths'] = []
            path = Path(tmp) / 'profiles.json'; path.write_text(json.dumps(raw))
            with patch('localocr.model_registry.DEFAULT_PROFILE_PATH', path):
                before = _request_variant('auto', None)
                original = next(p for p in raw['profiles'] if p['engine'] == 'vl')
                raw['profiles'].append({**original, 'id': 'candidate-vl', 'revision': 'new'})
                raw['defaults']['vl'] = 'candidate-vl'; path.write_text(json.dumps(raw))
                self.assertEqual(load_model_profiles().defaults['vl'], 'candidate-vl')
                self.assertNotEqual(before, _request_variant('auto', None))

    def test_weight_and_runtime_version_changes_invalidate_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'weights.bin'; path.write_bytes(b'old')
            profile = replace(resolve_model_reference('ocr'), artifact_paths=(str(path),))
            before = execution_sha256(profile)
            path.write_bytes(b'new weight bytes')
            self.assertNotEqual(before, execution_sha256(profile))
            with patch('localocr.release_identity._package_version', return_value='different'):
                self.assertNotEqual(before, execution_sha256(profile))

    def test_failure_preserves_completed_page_without_false_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'two.tiff'
            a, b = Image.new('RGB', (80, 60), 'white'), Image.new('RGB', (80, 60), 'black')
            a.save(path, save_all=True, append_images=[b])
            count = 0
            def predict(_):
                nonlocal count
                count += 1
                if count == 2: raise ValueError('second page failed')
                return {'pages': [{'blocks': [{'text': 'first'}]}]}
            engine = SimpleNamespace(engine_name='Fake', model_name='Fake', predict_image=predict)
            with patch('localocr.model_registry.get_engine', return_value=engine):
                with self.assertRaises(ExecutionError) as exc:
                    _predict({'device': 'cpu', 'profile_id': 'ppocrv6-medium', 'path': str(path), 'tmp_dir': tmp}, {}, lambda _: None)
            self.assertEqual(len(exc.exception.context['partial_result']['pages']), 1)
            self.assertEqual(exc.exception.context['partial_result']['expected_page_count'], 2)

    def test_cancel_http_response_is_terminal_and_non_retryable(self):
        from localocr.server import _error_response
        payload = json.loads(_error_response(ExecutionError('execution_cancelled', 'cancelled', http_status=409)).body)
        self.assertEqual(payload['status'], 'cancelled')
        self.assertFalse(payload['retryable'])
        self.assertEqual(payload['recommendation'], 'stop_user_cancelled')

if __name__ == '__main__':
    unittest.main()