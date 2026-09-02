from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localocr.outputs import (
    build_display_summary,
    write_isolated_projections,
    write_outputs,
)


class OutputTest(unittest.TestCase):
    def test_display_summary_projects_objective_state_without_changing_it(self) -> None:
        result = {
            "objective_outcome": "text_detected",
            "execution_status": "completed",
            "execution": {"status": "completed"},
            "coverage": {"status": "complete"},
            "quality": {"status": "sufficient", "flags": []},
            "route": {"effective_engine": "ocr", "escalated": False},
            "pages": [
                {
                    "page_index": 0,
                    "blocks": [
                        {"text": "one", "score": 0.9},
                        {"text": "two", "score": 0.8},
                    ],
                }
            ],
        }

        summary = build_display_summary(result)

        self.assertEqual(summary["status"], "text_detected")
        self.assertEqual(summary["text_block_count"], 2)
        self.assertEqual(summary["mean_score"], 0.85)
        self.assertFalse(summary["route_escalated"])
        self.assertEqual(summary["warnings"], [])
        self.assertEqual(result["objective_outcome"], "text_detected")

    def test_empty_blocks_never_make_display_summary_claim_no_text(self) -> None:
        result = {
            "execution_status": "completed",
            "coverage": {"status": "complete"},
            "quality": {"status": "sufficient", "flags": []},
            "pages": [{"page_index": 0, "blocks": []}],
        }

        summary = build_display_summary(result)

        self.assertEqual(summary["status"], "indeterminate")
        self.assertNotIn("已验证未检测到文字", summary["message"])

        result["objective_outcome"] = "no_text_detected"
        verified = build_display_summary(result)
        self.assertEqual(verified["status"], "no_text_detected")
        self.assertIn("已验证未检测到文字", verified["message"])

    def test_json_output_preserves_route_and_difficulty_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"image")
            result = {
                "engine": "Fake VL",
                "engine_key": "vl",
                "model": "Fake VL",
                "model_id": "paddleocr-vl-1.6",
                "device": "gpu:0",
                "route": {
                    "effective_engine": "vl",
                    "escalated": True,
                    "difficulty": {
                        "policy_version": "ocr-confidence-v1",
                        "reasons": ["low_score_ratio"],
                    },
                },
                "display_summary": {
                    "status": "text_detected",
                    "message": "识别完成：检测到 1 个文字块。",
                },
                "pages": [{"page_index": 0, "blocks": []}],
            }

            paths = write_outputs(result, source, root / "out")
            payload = json.loads(paths["json"].read_text(encoding="utf-8"))

            self.assertTrue(payload["route"]["escalated"])
            self.assertEqual(payload["route"]["effective_engine"], "vl")
            self.assertEqual(
                payload["route"]["difficulty"]["policy_version"],
                "ocr-confidence-v1",
            )
            self.assertEqual(payload["display_summary"]["status"], "text_detected")
            self.assertIn("摘要:", paths["txt"].read_text(encoding="utf-8"))
            self.assertIn("- 摘要:", paths["md"].read_text(encoding="utf-8"))

    def test_hash_isolated_projections_do_not_share_same_stem_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"image")
            result = {"engine": "Fake OCR", "pages": [{"page_index": 0, "blocks": []}]}
            first = write_isolated_projections(result, source, root / "out", request_hash="a" * 64)
            second = write_isolated_projections(result, source, root / "out", request_hash="b" * 64)

            self.assertNotEqual(first["canonical_json"], second["canonical_json"])
            self.assertTrue(first["canonical_json"].exists())
            self.assertTrue(second["canonical_json"].exists())


if __name__ == "__main__":
    unittest.main()
