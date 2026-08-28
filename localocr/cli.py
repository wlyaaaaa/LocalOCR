"""One-shot CLI using the same supervised execution and output contract as the API."""

from __future__ import annotations

import argparse
import sys

from .gpu_broker import GpuBrokerLease
from .runtime import DEFAULT_TIMEOUT_SEC, MAX_TIMEOUT_SEC
from .service import OCRService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="localocr", description="本地中文 OCR、版面、表格与公式识别。"
    )
    parser.add_argument("inputs", nargs="+", help="图片、PDF 或目录路径。")
    parser.add_argument(
        "--engine", choices=["auto", "ocr", "vl", "structure"], default="auto"
    )
    parser.add_argument(
        "--model", default=None, help="具体模型 profile；通常保持自动选择。"
    )
    parser.add_argument("--out-dir", default="outputs")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--tmp-dir", default="_pdf_pages")
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=DEFAULT_TIMEOUT_SEC,
        help="整个请求的执行期限（秒）；失败时终止工作进程并释放租约。",
    )
    parser.add_argument(
        "--no-gpu-probe", action="store_true", help="仅跳过硬件探针，不绕过GPU租约。"
    )
    args = parser.parse_args(argv)
    if not 0 < args.timeout_sec <= MAX_TIMEOUT_SEC:
        parser.error(f"--timeout-sec must be in (0, {MAX_TIMEOUT_SEC:g}]")
    gpu = args.device.lower().startswith(("gpu", "cuda"))
    service = OCRService(
        device=args.device,
        tmp_dir=args.tmp_dir,
        probe_on_start=not args.no_gpu_probe and gpu,
        gpu_lease_factory=(lambda _owner: GpuBrokerLease("localocr-cli"))
        if gpu
        else None,
    )
    try:
        response = service.process_inputs(
            args.inputs,
            engine_choice=args.engine,
            model_choice=args.model,
            recursive=args.recursive,
            out_dir=args.out_dir,
            timeout_sec=args.timeout_sec,
        )
        if not response["ok"]:
            print(
                f"[忙碌] {response.get('job_key') or ''}；请查询现有任务，不要重复提交。",
                file=sys.stderr,
            )
            return 3
        for index, result in enumerate(response["results"], 1):
            print(
                f"[{index}/{response['count']}] {result['source_file']} -> "
                f"{result['model_id']} | {result['output_files']['md']}",
                flush=True,
            )
        print(
            f"[完成] {response['count']} 个结果；{response.get('gpu') or args.device}",
            flush=True,
        )
        return 0
    except Exception as exc:
        print(
            f"[失败] {getattr(exc, 'code', type(exc).__name__)}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
