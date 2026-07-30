from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localocr.outputs import write_outputs


class OutputTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
