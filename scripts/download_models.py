#!/usr/bin/env python3
"""Populate model caches through the same bounded, brokered runtime as real work."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Explicit, serial model-cache warmup.")
    parser.add_argument("--allow-heavy", action="store_true")
    parser.add_argument("--timeout-sec", type=float, default=1200)
    args = parser.parse_args(argv)
    if not args.allow_heavy:
        parser.error("Model warmup runs heavy GPU work; pass --allow-heavy explicitly.")
    if not 0 < args.timeout_sec <= 7200:
        parser.error("--timeout-sec must be in (0, 7200].")

    os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
    os.environ.setdefault("PADDLE_PDX_DISABLE_DEV_MODEL_WL", "true")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "true")
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")
    project = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project))
    from localocr.service import OCRService

    with tempfile.TemporaryDirectory(prefix="localocr-model-warmup-") as work:
        service = OCRService(tmp_dir=Path(work) / "pages", job_dir=Path(work) / "jobs")
        try:
            for engine, sample in (("ocr", "probe_text.png"), ("vl", "probe_text.png"),
                                   ("structure", "sample_table.png")):
                response = service.process_inputs(
                    [project / "tests" / "samples" / sample], engine_choice=engine,
                    write_files=False, timeout_sec=args.timeout_sec,
                )
                if not response["ok"]:
                    raise RuntimeError(response.get("status") or "Model warmup failed.")
                print(f"{engine}: ready; {service.gpu_summary}", flush=True)
        finally:
            service.close()
    print("Model caches ready. This is not a transcription-accuracy certificate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
