from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .gpu_probe import probe_gpu, format_probe, GPUProbeError
from .router import collect_files, route_engine, is_pdf
from .engines import get_engine
from .outputs import write_outputs
from .pdf_utils import render_pdf_to_files


def _ocr_pdf_with_ocr_engine(pdf_path: Path, engine, tmp_dir: Path) -> dict:
    """用 PP-OCRv6 处理 PDF：先把每页转图，再逐页 OCR。"""
    images = render_pdf_to_files(pdf_path, out_dir=tmp_dir)
    pages = []
    for i, img in enumerate(images):
        r = engine.predict_image(str(img))
        for p in r.get("pages", []):
            p["page_index"] = i
            pages.append(p)
    return {
        "engine": "PP-OCRv6_medium",
        "model": engine.model_name,
        "device": engine.device,
        "pages": pages,
    }


def _ocr_pdf_with_vl(pdf_path: Path, engine, tmp_dir: Path) -> dict:
    """用 VL 处理 PDF：逐页转图后送 VL。"""
    images = render_pdf_to_files(pdf_path, out_dir=tmp_dir)
    pages = []
    for i, img in enumerate(images):
        r = engine.predict_image(str(img))
        for p in r.get("pages", []):
            p["page_index"] = i
            pages.append(p)
    return {
        "engine": "PaddleOCR-VL-1.6",
        "model": engine.model_name,
        "device": engine.device,
        "pages": pages,
    }


def process_one(path: Path, engine_choice: str, device: str, tmp_dir: Path,
                engine_cache: dict) -> dict:
    eng_key = route_engine(path, engine_choice)
    if eng_key not in engine_cache:
        engine_cache[eng_key] = get_engine(eng_key, device=device)
    engine = engine_cache[eng_key]
    if is_pdf(path):
        if eng_key == "vl":
            result = _ocr_pdf_with_vl(path, engine, tmp_dir)
        else:
            result = _ocr_pdf_with_ocr_engine(path, engine, tmp_dir)
    else:
        result = engine.predict_image(str(path))
    result["source_file"] = str(path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="localocr",
        description="本地高质量中文 OCR：图片用 PP-OCRv6_medium，PDF/复杂文档用 PaddleOCR-VL-1.6，自动分流。",
    )
    parser.add_argument("inputs", nargs="+", help="图片 / PDF / 文件夹路径。")
    parser.add_argument("--engine", choices=["auto", "ocr", "vl"], default="auto",
                        help="auto=自动分流(默认)；ocr=强制PP-OCRv6_medium；vl=强制PaddleOCR-VL-1.6。")
    parser.add_argument("--out-dir", default="outputs", help="输出目录(默认 outputs)。")
    parser.add_argument("--recursive", action="store_true", help="文件夹递归扫描。")
    parser.add_argument("--device", default="gpu:0", help="设备(默认 gpu:0)。")
    parser.add_argument("--tmp-dir", default="_pdf_pages", help="PDF 转图临时目录。")
    parser.add_argument("--no-gpu-probe", action="store_true", help="跳过启动GPU探针(不建议)。")
    args = parser.parse_args()

    if not args.no_gpu_probe:
        try:
            info = probe_gpu()
            print(f"[GPU] {format_probe(info)}", flush=True)
        except GPUProbeError as e:
            print(f"[GPU 探针失败] {e}", file=sys.stderr)
            print("拒绝静默回退 CPU，已退出。请检查 WSL2 + NVIDIA 驱动 + cu129 wheel。", file=sys.stderr)
            return 2

    files = collect_files(args.inputs, args.recursive)
    if not files:
        print("未找到可识别的文件(支持 png/jpg/jpeg/bmp/webp/tif/tiff/pdf)。", file=sys.stderr)
        return 1
    print(f"[收集] 共 {len(files)} 个文件待识别", flush=True)

    out_dir = Path(args.out_dir)
    tmp_dir = Path(args.tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    engine_cache: dict = {}
    ok, fail = 0, 0
    for i, f in enumerate(files, 1):
        t0 = time.time()
        try:
            eng_key = route_engine(f, args.engine)
            result = process_one(f, args.engine, args.device, tmp_dir, engine_cache)
            paths = write_outputs(result, f, out_dir)
            dt = time.time() - t0
            nblocks = sum(len(p.get("blocks", [])) for p in result.get("pages", []))
            print(f"[{i}/{len(files)}] {f.name} -> 引擎={eng_key} | {nblocks}块 | {dt:.1f}s | "
                  f"{paths['md'].name}", flush=True)
            ok += 1
        except Exception as e:
            dt = time.time() - t0
            print(f"[{i}/{len(files)}] {f.name} 失败 [{type(e).__name__}: {e}] ({dt:.1f}s)", file=sys.stderr, flush=True)
            fail += 1
    print(f"[完成] 成功 {ok}，失败 {fail}，输出目录: {out_dir.resolve()}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
