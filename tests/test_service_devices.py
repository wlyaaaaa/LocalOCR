from __future__ import annotations
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import Mock, patch
from localocr.gpu_broker import GpuBrokerConflict, GpuBrokerError, GpuBrokerLeaseLost
from localocr.runtime import ExecutionError
from localocr.service import OCRService


class Runtime:
    def __init__(self, score=0.99):
        self.calls, self.closed, self.score, self.fail = [], 0, score, False
        self.pid, self.profile_id, self.loaded, self.device = None, None, False, None
        self.gpu_summary, self.peak_memory_bytes = None, 0

    def predict(self, payload, **_):
        self.calls.append(payload)
        if self.fail:
            raise ExecutionError('inference_failed', 'fake failure')
        self.pid, self.profile_id, self.loaded, self.device = 123, payload['profile_id'], True, payload['device']
        profile = payload['model_profile']
        return {'engine': profile['result_engine_name'], 'model': profile['display_name'],
                'engine_key': profile['engine'], 'model_id': profile['id'], 'device': payload['device'],
                'pages': [{'page_index': 0, 'blocks': [{'type': 'text', 'text': 'synthetic', 'score': self.score}]}]}

    def close(self):
        self.closed += 1
        self.pid, self.loaded, self.device = None, False, None


class DeviceTests(unittest.TestCase):
    def service(self, root, **kwargs):
        kwargs.setdefault('runtime', Runtime())
        kwargs.setdefault('vl_runtime', Runtime())
        kwargs.setdefault('gpu_lease_factory', Mock(side_effect=lambda _: nullcontext()))
        service = OCRService(tmp_dir=root / 'tmp', job_dir=root / 'jobs', probe_on_start=False, **kwargs)
        self.addCleanup(service.close)
        return service

    def sources(self, root):
        root.mkdir(parents=True, exist_ok=True)
        sample = (Path(__file__).parent / 'samples/probe_text.png').read_bytes()
        files = [root / name for name in ('one.png', 'two.png')]
        for index, path in enumerate(files):
            path.write_bytes(sample + bytes([index]))
        return files

    def test_auto_conflict_and_unavailable_stick_to_cpu_and_mark_skipped_upgrade(self):
        for error, reason in ((GpuBrokerConflict('busy', owner='video-scaffold'), 'gpu_busy:video-scaffold'),
                              (GpuBrokerError('offline'), 'gpu_broker_unavailable')):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                lease = Mock(side_effect=error)
                service = self.service(root, runtime=Runtime(score=0.1), gpu_lease_factory=lease)
                results = service.process_inputs(self.sources(root), write_files=False)['results']
                self.assertEqual(lease.call_count, 1)
                self.assertEqual([p['device'] for p in service._runtime.calls], ['cpu', 'cpu'])
                self.assertEqual(service._vl_runtime.calls, [])
                for result in results:
                    self.assertEqual(result['route']['device_reason'], reason)
                    self.assertEqual(result['route']['device'], 'cpu')
                    self.assertFalse(result['route']['escalated'])
                    self.assertEqual(result['route']['escalation_skipped'], {'reason': 'gpu_unavailable', 'page_indices': [0]})
                    self.assertNotEqual(result['quality']['status'], 'sufficient')
                    self.assertIn('escalation_skipped_gpu_unavailable', result['display_summary']['warnings'])
                    self.assertIn('需要升级的页因显卡被占用未升级', result['display_summary']['message'])

    def test_vl_gpu_policy_and_lease_lost_never_fall_back(self):
        cases = [('vl', 'auto', GpuBrokerConflict('busy')), ('structure', 'auto', GpuBrokerConflict('busy')),
                 ('ocr', 'gpu', GpuBrokerConflict('busy')), ('ocr', 'auto', GpuBrokerLeaseLost('lost'))]
        for engine, device, error in cases:
            with self.subTest(engine=engine, device=device, error=type(error)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                service = self.service(root, gpu_lease_factory=Mock(side_effect=error))
                with self.assertRaises(type(error)):
                    service.process_inputs(self.sources(root)[:1], engine_choice=engine, device=device, write_files=False)
                self.assertEqual(service._runtime.calls + service._vl_runtime.calls, [])

    def test_cpu_policy_and_cpu_constructor_never_construct_lease(self):
        for configured, requested in (('gpu:0', 'cpu'), ('cpu', 'gpu')):
            with tempfile.TemporaryDirectory() as tmp:
                root, lease = Path(tmp), Mock(side_effect=AssertionError('lease used on CPU'))
                service = self.service(root, device=configured, gpu_lease_factory=lease)
                result = service.process_inputs(self.sources(root)[:1], engine_choice='ocr', device=requested, write_files=False)['results'][0]
                lease.assert_not_called()
                self.assertEqual(result['route']['device'], 'cpu')
                self.assertEqual(result['route']['device_reason'], 'requested')

    def test_release_failure_after_claim_cache_hit_closes_both_slots_and_unlocks(self):
        @contextmanager
        def lease(_):
            yield None
            raise GpuBrokerError('release failed')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.service(root, gpu_lease_factory=lease)
            with patch.object(service.job_registry, 'try_claim', return_value=Mock(kind='cache_hit', response={})), \
                 self.assertRaises(GpuBrokerError):
                service.process_inputs(self.sources(root)[:1], engine_choice='ocr', out_dir=root / 'out')
            self.assertFalse(service._execution_lock.locked())
            self.assertGreater(service._runtime.closed, 0)
            self.assertGreater(service._vl_runtime.closed, 0)

    def test_device_keys_and_cache_lookup_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, service = self.sources(root), self.service(root)
            results = [service.process_inputs(files[:1], engine_choice='ocr', device=policy, write_files=False)['results'][0]
                       for policy in ('gpu', 'cpu')]
            self.assertNotEqual(results[0]['job_key'], results[1]['job_key'])
            for policy, expected in (('gpu', ['gpu:0']), ('cpu', ['cpu']), ('auto', ['gpu:0'])):
                with patch.object(service.job_registry, 'cached_response', return_value=None) as cache:
                    service.process_inputs(files[:1], engine_choice='ocr', device=policy, out_dir=root / policy)
                    self.assertEqual([call.args[0].request_variant.split('device=')[1].split(';')[0] for call in cache.call_args_list], expected)
            service._gpu_lease_factory = Mock(side_effect=GpuBrokerConflict('busy'))
            with patch.object(service.job_registry, 'cached_response', return_value=None) as cache:
                service.process_inputs(files, engine_choice='ocr', out_dir=root / 'sticky')
                self.assertEqual([call.args[0].request_variant.split('device=')[1].split(';')[0] for call in cache.call_args_list],
                                 ['gpu:0', 'cpu', 'gpu:0', 'cpu'])

    def test_vl_slot_upgrade_loaded_order_cleanup_and_shared_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.service(root, runtime=Runtime(score=0.1))
            files = self.sources(root)[:1]
            service.process_inputs(files, engine_choice='vl', write_files=False)
            self.assertEqual(len(service._runtime.calls), 0)
            self.assertEqual(len(service._vl_runtime.calls), 1)
            service.process_inputs(files, write_files=False)
            self.assertEqual(len(service._runtime.calls), 1)
            self.assertEqual(len(service._vl_runtime.calls), 2)
            self.assertEqual(service.loaded_models, ['ppocrv6-medium', 'paddleocr-vl-1.6'])
            self.assertEqual(service.loaded_engines, ['ocr', 'vl'])
            service._vl_runtime.fail = True
            with self.assertRaises(ExecutionError):
                service.process_inputs(files, write_files=False)
            self.assertGreater(service._runtime.closed, 0)
            self.assertGreater(service._vl_runtime.closed, 0)
            shared = self.service(root / 'shared', vl_runtime=None)
            self.assertIs(shared._runtime, shared._vl_runtime)
