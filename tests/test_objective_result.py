from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localocr.objective_result import (
    annotate_result,
    config_sha256,
    file_sha256,
    validate_objective_sidecar,
    write_objective_sidecar,
)


def write_gray_png(path: Path, width: int = 16, height: int = 16, value: int = 0) -> None:
    rows = b"".join(b"\x00" + bytes([value]) * width for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    payload = b"\x89PNG\r\n\x1a\n"
    payload += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
    payload += chunk(b"IDAT", zlib.compress(rows))
    payload += chunk(b"IEND", b"")
    path.write_bytes(payload)


def write_pattern_png(path: Path, width: int = 16, height: int = 16) -> None:
    rows = []
    for row_index in range(height):
        row = bytearray(width)
        row[(row_index * 3) % width] = 255
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    payload = b"\x89PNG\r\n\x1a\n"
    payload += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
    payload += chunk(b"IDAT", zlib.compress(raw))
    payload += chunk(b"IEND", b"")
    path.write_bytes(payload)


class ObjectiveResultTest(unittest.TestCase):
    def test_empty_blocks_are_indeterminate_not_no_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "camera.jpg"
            source.write_bytes(b"not-a-real-image-but-nonempty")
            result = annotate_result(
                {"engine_key": "ocr", "pages": [{"page_index": 0, "blocks": []}]},
                source,
                processor="fake:ocr",
                model_id="fake-ocr",
                pipeline_version="test",
                config={},
            )

            self.assertEqual(result["objective_outcome"], "indeterminate")
            self.assertEqual(result["execution_status"], "corrupt")
            self.assertNotEqual(result["objective_outcome"], "no_text_detected")
            self.assertIn("empty_observation", result["quality"]["flags"])
            self.assertIsNone(result["objective_result"]["negative_evidence"])

    def test_uniform_image_gets_independent_no_text_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "blank.png"
            write_gray_png(source)
            result = annotate_result(
                {"engine_key": "ocr", "pages": [{"page_index": 0, "blocks": []}]},
                source,
                processor="fake:ocr",
                model_id="fake-ocr",
                pipeline_version="test",
                config={"lang": "ch"},
                request_hash="r" * 64,
            )

            self.assertEqual(result["objective_outcome"], "no_text_detected")
            self.assertEqual(result["execution_status"], "completed")
            self.assertEqual(result["quality"]["status"], "sufficient")
            negative = result["objective_result"]["negative_evidence"]
            self.assertIsInstance(negative, dict)
            self.assertGreater(negative["size_bytes"], 0)
            self.assertEqual(
                negative["sha256"],
                __import__("hashlib").sha256(negative["artifact"].encode("utf-8")).hexdigest(),
            )

    def test_nonuniform_empty_image_stays_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "photo.png"
            write_pattern_png(source)
            result = annotate_result(
                {"engine_key": "vl", "pages": [{"page_index": 0, "blocks": []}]},
                source,
                processor="fake:vl",
                model_id="fake-vl",
                pipeline_version="test",
                config={},
            )

            self.assertEqual(result["objective_outcome"], "indeterminate")
            self.assertIsNone(result["objective_result"]["negative_evidence"])

    def test_low_confidence_is_quality_not_execution_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "photo.jpg"
            source.write_bytes(b"photo-bytes")
            result = annotate_result(
                {
                    "engine_key": "ocr",
                    "pages": [
                        {
                            "page_index": 0,
                            "blocks": [{"text": "可疑", "score": 0.2}],
                        }
                    ],
                },
                source,
                processor="fake:ocr",
                model_id="fake-ocr",
                pipeline_version="test",
                config={},
            )

            self.assertEqual(result["objective_outcome"], "text_detected")
            self.assertEqual(result["execution_status"], "completed")
            self.assertEqual(result["quality"]["status"], "low_confidence")
            self.assertIn("low_confidence", result["quality"]["flags"])

    def test_explicit_negative_evidence_does_not_override_partial_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "scan.png"
            write_gray_png(source)
            result = annotate_result(
                {"engine_key": "ocr", "pages": []},
                source,
                processor="fake:ocr",
                model_id="fake-ocr",
                pipeline_version="test",
                config={},
                no_text_evidence={"method": "trusted-detector"},
            )

            self.assertEqual(result["objective_outcome"], "indeterminate")
            self.assertEqual(result["coverage"]["status"], "unknown")
            self.assertIsNone(result["objective_result"]["negative_evidence"])

    def test_pdf_pages_keep_image_media_kind_and_document_source_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "scan.pdf"
            source.write_bytes(b"%PDF-1.7 placeholder")
            result = annotate_result(
                {
                    "engine_key": "ocr",
                    "expected_page_count": 1,
                    "pages": [{"page_index": 0, "blocks": [{"text": "条款"}]}],
                },
                source,
                processor="fake:ocr",
                model_id="fake-ocr",
                pipeline_version="test",
                config={},
            )

            self.assertEqual(result["objective_result"]["media_kind"], "image")
            self.assertEqual(result["objective_result"]["source_format"], "pdf")

    def test_cache_sidecar_rejects_zero_byte_or_tampered_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "blank.png"
            write_gray_png(source)
            result = annotate_result(
                {"engine_key": "ocr", "pages": [{"page_index": 0, "blocks": []}]},
                source,
                processor="fake:ocr",
                model_id="fake-ocr",
                pipeline_version="test",
                config={"lang": "ch"},
                request_hash="q" * 64,
            )
            sidecar, sidecar_hash = write_objective_sidecar(
                result["objective_result"],
                source,
                root / "out",
                request_hash="q" * 64,
            )
            self.assertTrue(
                validate_objective_sidecar(
                    sidecar,
                    request_hash="q" * 64,
                    raw_sha256=file_sha256(source),
                    profile_id="fake-ocr",
                    engine="ocr",
                    config_sha256_value=config_sha256({"lang": "ch"}),
                    expected_file_sha256=sidecar_hash,
                )
            )

            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            payload["identity"]["raw_sha256"] = "0" * 64
            sidecar.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.assertFalse(
                validate_objective_sidecar(
                    sidecar,
                    request_hash="q" * 64,
                    raw_sha256=file_sha256(source),
                    profile_id="fake-ocr",
                    engine="ocr",
                    config_sha256_value=config_sha256({"lang": "ch"}),
                )
            )


if __name__ == "__main__":
    unittest.main()
