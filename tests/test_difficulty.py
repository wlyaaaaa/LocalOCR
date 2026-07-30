from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class OCRDifficultyTest(unittest.TestCase):
    def test_empty_text_escalates(self) -> None:
        from localocr.difficulty import assess_ocr_difficulty

        assessment = assess_ocr_difficulty({"pages": [{"blocks": []}]})

        self.assertTrue(assessment.should_escalate)
        self.assertIn("empty_text", assessment.reasons)

    def test_low_score_ratio_escalates(self) -> None:
        from localocr.difficulty import assess_ocr_difficulty

        assessment = assess_ocr_difficulty(
            {
                "pages": [
                    {
                        "blocks": [
                            {"text": "可信", "score": 0.99},
                            {"text": "可疑", "score": 0.42},
                            {"text": "乱码", "score": 0.31},
                            {"text": "正常", "score": 0.97},
                        ]
                    }
                ]
            }
        )

        self.assertTrue(assessment.should_escalate)
        self.assertIn("low_score_ratio", assessment.reasons)
        self.assertEqual(assessment.metrics["text_block_count"], 4)
        self.assertEqual(assessment.metrics["scored_block_count"], 4)

    def test_high_confidence_text_stays_on_ocr(self) -> None:
        from localocr.difficulty import assess_ocr_difficulty

        assessment = assess_ocr_difficulty(
            {
                "pages": [
                    {
                        "blocks": [
                            {"text": "中文", "score": 0.99},
                            {"text": "识别", "score": 0.96},
                            {"text": "正常", "score": 0.91},
                        ]
                    }
                ]
            }
        )

        self.assertFalse(assessment.should_escalate)
        self.assertEqual(assessment.reasons, ())

    def test_unscored_nonempty_output_does_not_guess(self) -> None:
        from localocr.difficulty import assess_ocr_difficulty

        assessment = assess_ocr_difficulty(
            {"pages": [{"blocks": [{"text": "已有文本", "score": None}]}]}
        )

        self.assertFalse(assessment.should_escalate)
        self.assertEqual(assessment.metrics["scored_block_count"], 0)


if __name__ == "__main__":
    unittest.main()
