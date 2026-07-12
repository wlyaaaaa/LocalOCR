#!/usr/bin/env python3
"""对 4 份合成样本各跑一次识别，采集模型/GPU/显存/速度/输出片段，写 TEST_REPORT.md（需求 11）。"""
from __future__ import annotations

import json
import argparse
import atexit
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
os.environ.setdefault("PADDLE_PDX_DISABLE_DEV_MODEL_WL", "true")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "true")
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localocr.gpu_probe import probe_gpu, format_probe
from localocr.router import route_engine
from localocr.engines import get_engine
from localocr.outputs import write_outputs
from localocr.pdf_utils import render_pdf_to_images
from localocr.cli import _ocr_pdf_with_ocr_engine, _ocr_pdf_with_vl
from localocr.gpu_broker import GpuBrokerLease

SAMPLES = Path(__file__).resolve().parent / "samples"
OUT = Path(__file__).resolve().parent / "outputs"
TMP = Path(__file__).resolve().parent / "_pdf_pages"

CASES = [
    ("中文截图", "sample_chat_screenshot.png", "ocr"),
    ("扫描PDF", "sample_scan.pdf", "vl"),
    ("表格", "sample_table.png", "vl"),
    ("结构化表格", "sample_table.png", "structure"),
    ("公式文档", "sample_formula.png", "vl"),
]


def gpu_mem() -> str:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() + " MiB"
    except Exception:
        return "?"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the heavy LocalOCR GPU integration suite.")
    parser.add_argument(
        "--allow-heavy",
        action="store_true",
        help="Explicitly authorize loading OCR, VL, and Structure GPU models and writing test outputs.",
    )
    args = parser.parse_args(argv)
    if not args.allow_heavy:
        parser.error("heavy GPU integration requires explicit --allow-heavy authorization")
    OUT.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    lease = GpuBrokerLease("localocr-integration-test")
    lease.__enter__()
    atexit.register(lease.__exit__, None, None, None)
    report = ["# LocalOCR 测试报告\n", f"日期：{time.strftime('%Y-%m-%d %H:%M')}\n"]
    info = probe_gpu()
    report.append(f"\n## GPU 环境\n\n- {format_probe(info)}\n- 推理前显存：{gpu_mem()}\n")

    cache = {}
    results = []
    for title, fname, expect in CASES:
        p = SAMPLES / fname
        if not p.exists():
            report.append(f"\n## {title}\n\n[样本缺失] {p}\n")
            continue
        # 测试时强制走期望引擎，以验证两个引擎都能处理对应类型
        eng_key = expect
        mem0 = gpu_mem()
        t0 = time.time()
        try:
            if p.suffix.lower() == ".pdf":
                engine = get_engine(eng_key, device="gpu:0")
                if eng_key == "vl":
                    res = _ocr_pdf_with_vl(p, engine, TMP)
                else:
                    res = _ocr_pdf_with_ocr_engine(p, engine, TMP)
            else:
                engine = get_engine(eng_key, device="gpu:0")
                res = engine.predict_image(str(p))
            dt = time.time() - t0
            mem1 = gpu_mem()
            paths = write_outputs(res, p, OUT)
            pages = res.get("pages", [])
            nblocks = sum(len(pg.get("blocks", [])) for pg in pages)
            sample_texts = []
            for pg in pages[:2]:
                for b in pg.get("blocks", [])[:8]:
                    t = (b.get("text") or "").strip()
                    if t:
                        sample_texts.append(t)
            report.append(f"\n## {title}\n")
            report.append(f"- 文件：`{fname}`\n")
            report.append(f"- 引擎：`{eng_key}`\n")
            report.append(f"- 模型：`{res.get('model')}`\n")
            report.append(f"- 耗时：{dt:.2f}s\n")
            report.append(f"- 显存：{mem0} → {mem1}\n")
            report.append(f"- 页数：{len(pages)}，块数：{nblocks}\n")
            report.append(f"- 输出：`{paths['md'].name}` / `{paths['json'].name}`\n")
            report.append(f"- 方向角度：{res.get('page_angle')}\n")
            report.append(f"\n### 识别文本片段\n\n```\n" + "\n".join(sample_texts[:10]) + "\n```\n")
            results.append((title, True, dt))
        except Exception as e:
            dt = time.time() - t0
            report.append(f"\n## {title}\n\n[失败] {type(e).__name__}: {e} ({dt:.2f}s)\n")
            results.append((title, False, dt))

    report.append(f"\n## 汇总\n\n| 样本 | 结果 | 耗时 |\n|---|---|---|\n")
    for title, ok, dt in results:
        report.append(f"| {title} | {'✓' if ok else '✗'} | {dt:.1f}s |\n")
    report.append(f"\n推理后显存：{gpu_mem()}\n")

    rp = Path(__file__).resolve().parent / "TEST_REPORT.md"
    rp.write_text("".join(report), encoding="utf-8")
    print("报告已写入:", rp)
    print("".join(report[-15:]))


if __name__ == "__main__":
    main()
