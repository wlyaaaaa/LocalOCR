#!/usr/bin/env python3
"""Serial, cache-free GPU acceptance using generated non-private ground truth."""
from __future__ import annotations
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid


def make_cases(directory: Path):
    from PIL import Image, ImageDraw, ImageFont
    fonts = [Path(p) for p in ('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc', '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc')]
    selected = next((p for p in fonts if p.is_file()), None)
    if selected is None:
        raise RuntimeError('A local CJK font is required; missing glyphs are not valid OCR ground truth')
    def page(lines, size=32):
        font = ImageFont.truetype(str(selected), size)
        image = Image.new('RGB', (1000, max(360, 60 + len(lines) * 65)), 'white')
        draw = ImageDraw.Draw(image)
        for i, line in enumerate(lines):
            box = draw.textbbox((30, 30 + i * 65), line, font=font)
            if box[2] >= image.width - 20 or box[3] >= image.height - 20:
                raise ValueError('Generated ground truth would be clipped')
            draw.text((30, 30 + i * 65), line, font=font, fill='black')
        return image
    lines = ['本地文字识别验收', '编号 OCR-2026-0917', '数量 25 金额 1280.50', '请保留原始文字与页码']
    other = ['第二页独立核验', '编号 CHECK-7319', '实付金额 98.76 元']
    image, second = page(lines), page(other)
    image.save(directory / 'plain.png')
    rotated = image.rotate(90, expand=True); rotated.save(directory / 'rotated.png'); rotated.close()
    small_lines = ['小字核验 QX-7319', '实付金额 98.76 元']
    small = page(small_lines, 20); small.save(directory / 'small.png'); small.close()
    image.save(directory / 'two.tiff', save_all=True, append_images=[second])
    image.save(directory / 'two.pdf', save_all=True, append_images=[second], resolution=144)
    blank = Image.new('RGB', (600, 400), 'white'); blank.save(directory / 'blank.png'); blank.close()
    table = Image.new('RGB', (850, 400), 'white'); draw = ImageDraw.Draw(table); font = ImageFont.truetype(str(selected), 28)
    rows = [['项目', '数量', '金额'], ['样品A', '2', '30.00'], ['样品B', '3', '45.00']]
    for y in [50, 130, 210, 290]: draw.line((30, y, 810, y), fill='black', width=3)
    for x in [30, 290, 550, 810]: draw.line((x, 50, x, 290), fill='black', width=3)
    for y, row in enumerate(rows):
        for x, value in enumerate(row): draw.text((50 + 260 * x, 75 + 80 * y), value, font=font, fill='black')
    table.save(directory / 'table.png'); table.close()
    image.close(); second.close()
    return [
        {'name': 'plain', 'file': 'plain.png', 'engine': 'ocr', 'text': '\n'.join(lines), 'critical': ['OCR-2026-0917', '1280.50'], 'pages': 1, 'cer_max': .02},
        {'name': 'rotated', 'file': 'rotated.png', 'engine': 'ocr', 'text': '\n'.join(lines), 'critical': ['1280.50'], 'pages': 1, 'cer_max': .02},
        {'name': 'small', 'file': 'small.png', 'engine': 'ocr', 'text': '\n'.join(small_lines), 'critical': ['QX-7319', '98.76'], 'pages': 1, 'cer_max': .04},
        {'name': 'multiframe_tiff', 'file': 'two.tiff', 'engine': 'ocr', 'text': '\n'.join(lines + other), 'critical': ['OCR-2026-0917', 'CHECK-7319', '98.76'], 'pages': 2, 'cer_max': .02},
        {'name': 'blank', 'file': 'blank.png', 'engine': 'ocr', 'text': '', 'critical': [], 'pages': 1, 'cer_max': 0},
        {'name': 'vl_plain', 'file': 'plain.png', 'engine': 'vl', 'text': '\n'.join(lines), 'critical': ['OCR-2026-0917', '1280.50'], 'pages': 1, 'cer_max': .04},
        {'name': 'vl_pdf', 'file': 'two.pdf', 'engine': 'vl', 'text': '\n'.join(lines + other), 'critical': ['CHECK-7319', '98.76'], 'pages': 2, 'cer_max': .04},
        {'name': 'structure_table', 'file': 'table.png', 'engine': 'structure', 'critical': ['30.00', '45.00'], 'cells': [v for row in rows for v in row], 'pages': 1},
        {'name': 'auto_table', 'file': 'table.png', 'engine': 'auto', 'critical': ['30.00', '45.00'], 'pages': 1, 'escalation_required': True},
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Run serial synthetic GPU quality and coverage acceptance.')
    parser.add_argument('--allow-heavy', action='store_true')
    parser.add_argument('--out-dir', type=Path)
    parser.add_argument('--engine', choices=['ocr', 'vl', 'structure', 'auto'])
    parser.add_argument('--timeout-sec', type=float, default=180)
    args = parser.parse_args(argv)
    if not args.allow_heavy:
        parser.error('Heavy integration requires explicit --allow-heavy authorization')
    if not 0 < args.timeout_sec <= 7200: parser.error('Invalid execution timeout')
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root)); sys.path.insert(0, str(root / 'tests'))
    from quality_metrics import character_error_rate, normalize, result_text, table_cells
    from localocr.service import OCRService
    from localocr.release_identity import execution_sha256
    from localocr.model_registry import load_model_profiles
    for key, value in {'PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT': '0', 'PADDLE_PDX_DISABLE_DEV_MODEL_WL': 'true', 'PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK': 'true', 'PADDLE_PDX_MODEL_SOURCE': 'modelscope'}.items():
        os.environ.setdefault(key, value)
    output = args.out_dir or root / 'tests/outputs' / ('quality-' + uuid.uuid4().hex)
    output.mkdir(parents=True, exist_ok=True)
    report = {'schema': 'localocr.quality-acceptance.v1', 'python_prefix': sys.prefix, 'cache_mode': 'disabled', 'scope': 'synthetic regression, not a real-world accuracy ranking', 'cases': [], 'packages': {n: importlib.metadata.version(n) for n in ['paddlepaddle-gpu', 'paddleocr', 'paddlex']}}
    with tempfile.TemporaryDirectory(prefix='localocr-quality-') as temporary:
        work = Path(temporary); cases = make_cases(work)
        if args.engine: cases = [case for case in cases if case['engine'] == args.engine]
        service = OCRService(tmp_dir=work / 'scratch', job_dir=work / 'jobs')
        try:
            for case in cases:
                start = time.monotonic(); before = list(service.loaded_models)
                item = {'name': case['name'], 'engine': case['engine'], 'expected_pages': case['pages'], 'loaded_before': before, 'ok': False}
                try:
                    response = service.process_inputs([work / case['file']], engine_choice=case['engine'], write_files=False, timeout_sec=args.timeout_sec)
                    if not response['ok']: raise RuntimeError(response.get('error_code') or response.get('status'))
                    result = response['results'][0]; text = result_text(result)
                    cer = character_error_rate(case['text'], text) if 'text' in case else None
                    missing = [v for v in case['critical'] if normalize(v) not in normalize(text)]
                    cells = table_cells(result)
                    cells_ok = 'cells' not in case or cells == [normalize(v) for v in case['cells']]
                    coverage = result['objective_result']['coverage']
                    page_ok = coverage['status'] == 'complete' and coverage['pages_expected'] == case['pages'] and len(result['pages']) == case['pages']
                    route_ok = not case.get('escalation_required') or result.get('route', {}).get('escalated') is True
                    item.update(ok=page_ok and not missing and cells_ok and route_ok and (cer is None or cer <= case['cer_max']), cer=cer, cer_max=case.get('cer_max'), missing_critical=missing, table_cells=cells if 'cells' in case else None, table_cells_ok=cells_ok, coverage=coverage, route=result.get('route'), text=text, model_ids=result.get('models_used') or [result['model_id']], cache_status=result['cache_status'])
                    if result['cache_status'] != 'not_written': raise AssertionError('Benchmark unexpectedly used a result cache')
                except Exception as exc:
                    item.update(error=f'{type(exc).__name__}: {exc}', error_code=getattr(exc, 'code', None))
                item['elapsed_sec'] = round(time.monotonic() - start, 3)
                item['process_peak_rss_bytes'] = service._runtime.peak_memory_bytes
                report['cases'].append(item)
                print(json.dumps({key: item.get(key) for key in ['name', 'ok', 'cer', 'elapsed_sec', 'error']}, ensure_ascii=False), flush=True)
                (output / 'quality.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                if item.get('error_code') in {'execution_cancelled', 'gpu_lease_lost', 'broker_unavailable'}:
                    report['stopped_reason'] = item['error_code']
                    break
        finally:
            service.close()
    report['ok'] = bool(report['cases']) and all(case['ok'] for case in report['cases'])
    report['model_identities'] = {p.id: execution_sha256(p) for p in load_model_profiles().profiles.values()}
    (output / 'quality.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['ok'] else 1

if __name__ == '__main__':
    raise SystemExit(main())