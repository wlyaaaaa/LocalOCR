from __future__ import annotations
import json
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from fastapi.testclient import TestClient
from localocr import server
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import Mock, patch
from localocr import gpu_broker as broker
from localocr.gpu_broker import GpuBrokerConflict, GpuBrokerError, GpuBrokerLeaseLost, GpuBrokerTimeout, GpuBrokerUnavailable
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
        for error, reason, message in ((GpuBrokerConflict('busy', owner='video-scaffold'), 'gpu_busy:video-scaffold', '显卡被占用'),
                                       (GpuBrokerUnavailable('offline'), 'gpu_broker_unavailable', '无法连接显卡调度服务'),
                                       (GpuBrokerTimeout('late'), 'gpu_broker_timeout', '显卡调度服务答复超时'),
                                       (GpuBrokerConflict('unload failed', reason='resource_release_failed:TimeoutError'), 'gpu_broker_error', '显卡调度服务未能提供显卡')):
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
                    self.assertIn(f'需要升级的页因{message}未升级', result['display_summary']['message'])
                    self.assertEqual(result['route']['device_detail'], f'{type(error).__name__}: {error}')

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


@contextmanager
def delayed_broker(delay):
    calls, entered, released = [], threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            action = self.path.rsplit('/', 1)[-1]
            self.rfile.read(int(self.headers['Content-Length']))
            calls.append(action)
            if action == 'acquire':
                entered.set()
                time.sleep(delay)
            else:
                released.set()
            body = json.dumps({'ok': True, 'token': 'synthetic-token', 'owner': 'localocr'}).encode()
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', calls, entered, released
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


class BrokerWaitingTests(unittest.TestCase):
    service, sources = DeviceTests.service, DeviceTests.sources

    def setUp(self):
        identity = patch('localocr.service.execution_sha256', return_value='synthetic-identity')
        identity.start()
        self.addCleanup(identity.stop)

    def test_slow_reply_survives_old_limit_and_still_escalates(self):
        with delayed_broker(.08) as (url, calls, _, released), tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GpuBrokerTimeout):
                broker._urllib_transport(url, .02)('acquire', {})
            root = Path(tmp)
            service = self.service(root, runtime=Runtime(score=.1),
                                   gpu_lease_factory=lambda _: broker.GpuBrokerLease('localocr', base_url=url))
            with patch.object(broker, 'ACQUIRE_TIMEOUT_SECONDS', .4), \
                 patch.object(broker, 'default_transport', side_effect=broker._urllib_transport):
                result = service.process_inputs(self.sources(root)[:1], write_files=False)['results'][0]
            self.assertEqual(result['route']['device_reason'], 'gpu_available')
            self.assertTrue(result['route']['escalated'])
            self.assertEqual(service._runtime.calls[0]['device'], 'gpu:0')
            self.assertTrue(released.is_set())
            self.assertEqual(calls.count('acquire'), 2)  # Old-limit probe plus one new acquisition.

    def test_unresponsive_broker_leaves_time_for_cpu(self):
        with delayed_broker(1.5) as (url, calls, _, _), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.service(root, runtime=Runtime(score=.1),
                                   gpu_lease_factory=lambda _: broker.GpuBrokerLease('localocr', base_url=url))
            with patch.object(broker, 'ACQUIRE_TIMEOUT_SECONDS', .1), patch.object(broker, 'default_transport', side_effect=broker._urllib_transport):
                started = time.monotonic()
                result = service.process_inputs(self.sources(root)[:1], write_files=False, timeout_sec=1)['results'][0]
                self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(result['route']['device_reason'], 'gpu_broker_timeout')
            self.assertEqual(service._runtime.calls[0]['device'], 'cpu')
            self.assertIn('Timeout', result['route']['device_detail'])
            self.assertEqual(calls, ['acquire'])

    def test_refused_socket_falls_back_quickly(self):
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            url = f'http://127.0.0.1:{reserved.getsockname()[1]}'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.service(root, gpu_lease_factory=lambda _: broker.GpuBrokerLease('localocr', base_url=url))
            with patch.object(broker, 'default_transport', side_effect=broker._urllib_transport):
                started = time.monotonic()
                result = service.process_inputs(self.sources(root)[:1], write_files=False)['results'][0]
                self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(result['route']['device_reason'], 'gpu_broker_unavailable')
            self.assertEqual(service._runtime.calls[0]['device'], 'cpu')

    def test_cancelling_acquisition_releases_late_token(self):
        with delayed_broker(.25) as (url, calls, entered, released), tempfile.TemporaryDirectory() as tmp:
            root, errors = Path(tmp), []
            service = self.service(root, gpu_lease_factory=lambda _: broker.GpuBrokerLease('localocr', base_url=url))
            def run():
                try:
                    service.process_inputs(self.sources(root)[:1], write_files=False)
                except ExecutionError as exc:
                    errors.append(exc.code)
            with patch.object(broker, 'default_transport', side_effect=broker._urllib_transport):
                thread = threading.Thread(target=run)
                thread.start()
                self.assertTrue(entered.wait(5))
                self.assertTrue(service.cancel(service.active_jobs[0]['job_key']))
                thread.join(.2)
                self.assertFalse(thread.is_alive())
                self.assertTrue(released.wait(1))
            self.assertEqual(errors, ['execution_cancelled'])
            self.assertEqual(calls, ['acquire', 'release'])
            self.assertEqual(service._runtime.calls, [])


class ServiceControlTests(unittest.TestCase):
    service, sources, setUp = DeviceTests.service, DeviceTests.sources, BrokerWaitingTests.setUp

    def test_acquiring_status_overrides_missing_or_failed_and_old_key_cancels_cpu(self):
        for previous, error in ((p, e) for p in (None, 'failed') for e in (GpuBrokerTimeout('slow'), GpuBrokerUnavailable('offline'), GpuBrokerConflict('busy'))):
            with self.subTest(previous=previous, error=type(error)), tempfile.TemporaryDirectory() as tmp:
                root, old_keys = Path(tmp), []
                @contextmanager
                def lease(_):
                    key = client.get('/health').json()['active_jobs'][0]['job_key']
                    old_keys.append(key)
                    if previous:
                        service.job_registry.job_dir.mkdir(parents=True, exist_ok=True)
                        (root / 'jobs' / f'{key}.json').write_text(json.dumps({'status': previous}))
                    status = client.get(f'/jobs/{key}')
                    self.assertEqual(status.status_code, 200)
                    self.assertEqual(status.json()['status'], 'running')
                    self.assertEqual(status.json()['stage'], 'acquiring_gpu')
                    raise error
                    yield
                service = self.service(root, gpu_lease_factory=lease)
                service._runtime.memory_limit_bytes = 30_000_000_000
                def predict(*_args, **_kwargs):
                    key = old_keys[0]
                    self.assertNotEqual(key, service.active_jobs[0]['job_key'])
                    self.assertEqual(client.get(f'/jobs/{key}').json()['status'], 'running')
                    self.assertEqual(client.post(f'/jobs/{key}/cancel').status_code, 202)
                    service._checkpoint()
                service._runtime.predict = predict
                with patch.object(server, '_service', service), TestClient(server.app, base_url='http://127.0.0.1:18665') as client:
                    with self.assertRaises(ExecutionError) as raised:
                        service.process_inputs(self.sources(root)[:1], engine_choice='ocr', out_dir=root / 'out')
                self.assertEqual(raised.exception.code, 'execution_cancelled')

    def test_cached_paths_cannot_swallow_accepted_batch_cancellation(self):
        for branch, count in (('fallback', 2), ('claim', 2), ('claim', 1)):
            with self.subTest(branch=branch, count=count), tempfile.TemporaryDirectory() as tmp:
                root, accepted = Path(tmp), []
                @contextmanager
                def lease(_):
                    if branch == 'fallback':
                        raise GpuBrokerTimeout('synthetic timeout')
                    yield None
                    accepted.append(service.cancel(service.active_jobs[0]['job_key']))
                service = self.service(root, gpu_lease_factory=lease)
                build = service.job_registry.build_request
                def build_and_cancel(*args, **kwargs):
                    request = build(*args, **kwargs)
                    if branch == 'fallback' and 'device=cpu' in request.request_variant:
                        accepted.append(service.cancel(service.active_jobs[0]['job_key']))
                    return request
                cached = {'cache_status': 'cache_hit'}
                with patch.object(service.job_registry, 'build_request', side_effect=build_and_cancel), \
                     patch.object(service.job_registry, 'cached_response', side_effect=lambda r: cached if branch == 'fallback' and 'device=cpu' in r.request_variant else None), \
                     patch.object(service.job_registry, 'try_claim', return_value=Mock(kind='cache_hit', response=cached)):
                    with self.assertRaises(ExecutionError) as raised:
                        service.process_inputs(self.sources(root)[:count], engine_choice='ocr', out_dir=root / 'out')
                self.assertEqual(accepted, [True])
                self.assertEqual(raised.exception.code, 'execution_cancelled')
                self.assertEqual(service._runtime.calls, [])

    def test_release_timeout_keeps_completed_manifests_and_continues_batch(self):
        def transport(action, _payload):
            if action == 'release':
                raise GpuBrokerTimeout('synthetic release timeout')
            return {'ok': True, 'token': 'synthetic-token', 'owner': 'localocr'}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.service(root, gpu_lease_factory=lambda _: broker.GpuBrokerLease('localocr', transport=transport, renew_interval_seconds=0))
            result = service.process_inputs(self.sources(root), engine_choice='ocr', out_dir=root / 'out')
            self.assertTrue(result['ok'])
            self.assertEqual(len(service._runtime.calls), 2)
            self.assertEqual([service.job_registry.read_status(r['job_key'])['status'] for r in result['results']], ['completed', 'completed'])


if __name__ == '__main__':
    unittest.main()
