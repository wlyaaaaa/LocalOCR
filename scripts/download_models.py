#!/usr/bin/env python3
"""预下载所有模型到本地缓存，之后可完全离线运行（需求 7）。

模型来源：ModelScope（国内可达，不消耗代理流量，需求 8）。
下载后落到 ~/.paddlex/official_models/，后续 PaddleOCR/PaddleOCRVL 直接用本地缓存。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
os.environ.setdefault("PADDLE_PDX_DISABLE_DEV_MODEL_WL", "true")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "true")
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localocr.gpu_probe import probe_gpu, format_probe

print("[GPU]", format_probe(probe_gpu()))

from paddleocr import PaddleOCR, PaddleOCRVL, PPStructureV3

PROBE = str(Path(__file__).resolve().parent.parent / "tests" / "samples" / "probe_text.png")
STRUCTURE_PROBE = str(Path(__file__).resolve().parent.parent / "tests" / "samples" / "sample_table.png")
if not Path(PROBE).exists():
    Path(PROBE).parent.mkdir(parents=True, exist_ok=True)
    import io, cv2, numpy as np
    img = np.ones((200, 640, 3), dtype=np.uint8) * 255
    cv2.putText(img, "LocalOCR warmup", (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
    ok, arr = cv2.imencode(".png", img)
    Path(PROBE).write_bytes(arr.tobytes())

print("\n==== [1/3] 预热 PP-OCRv6_medium（检测+识别+方向+矫正） ====")
ocr = PaddleOCR(
    ocr_version="PP-OCRv6",
    lang="ch",
    use_doc_orientation_classify=True,
    use_doc_unwarping=True,
    use_textline_orientation=True,
    device="gpu:0",
)
r = ocr.predict(PROBE)
print("PP-OCRv6_medium OK:", r[0].json["res"].get("rec_texts"))

print("\n==== [2/3] 预热 PaddleOCR-VL-1.6 ====")
vl = PaddleOCRVL(
    pipeline_version="v1.6",
    vl_rec_backend="native",
    use_doc_orientation_classify=True,
    use_doc_unwarping=True,
    device="gpu:0",
)
r = vl.predict(PROBE)
blocks = r[0].json["res"].get("parsing_res_list", [])
print("PaddleOCR-VL-1.6 OK:", [b.get("block_content") for b in blocks][:3])

print("\n==== [3/3] 预热 PP-StructureV3 + PP-OCRv5 ====")
structure = PPStructureV3(
    lang="ch",
    ocr_version="PP-OCRv5",
    use_doc_orientation_classify=True,
    use_doc_unwarping=True,
    use_textline_orientation=True,
    use_table_recognition=True,
    use_formula_recognition=True,
    use_chart_recognition=False,
    use_seal_recognition=True,
    use_region_detection=True,
    format_block_content=True,
    device="gpu:0",
)
r = structure.predict(STRUCTURE_PROBE if Path(STRUCTURE_PROBE).exists() else PROBE)
blocks = r[0].json["res"].get("parsing_res_list", [])
print("PP-StructureV3 OK:", [b.get("block_label") for b in blocks][:5])

cache = Path.home() / ".paddlex" / "official_models"
print(f"\n==== 完成 ====")
print(f"模型缓存目录: {cache}")
if cache.exists():
    for d in sorted(cache.iterdir()):
        if d.is_dir():
            sz = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            print(f"  {d.name:40s} {sz / 1024 / 1024:8.1f} MB")
print("后续可完全离线运行。")
