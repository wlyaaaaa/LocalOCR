from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class SmartRouterTest(unittest.TestCase):
    def test_auto_routes_images_to_ocr_with_reason(self) -> None:
        from localocr.smart_router import choose_smart_route

        decision = choose_smart_route(Path("tests/samples/probe_text.png"), engine_choice="auto")

        self.assertEqual(decision.effective_engine, "ocr")
        self.assertEqual(decision.route_reason, "image_prefers_ocr")
        self.assertIn("image", decision.signals)
        self.assertGreaterEqual(decision.confidence, 0.8)

    def test_auto_routes_plain_pdf_to_ocr_for_fast_default(self) -> None:
        from localocr.smart_router import choose_smart_route

        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "address-confirmation.pdf"
            pdf.write_bytes(b"%PDF-1.7 tiny plain scan")

            decision = choose_smart_route(pdf, engine_choice="auto")

        self.assertEqual(decision.effective_engine, "ocr")
        self.assertEqual(decision.route_reason, "pdf_plain_text_prefers_ocr")
        self.assertIn("pdf", decision.signals)
        self.assertIn("plain_pdf_default", decision.signals)

    def test_auto_routes_complex_pdf_name_to_vl(self) -> None:
        from localocr.smart_router import choose_smart_route

        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "lecture-formula-table-layout.pdf"
            pdf.write_bytes(b"%PDF-1.7 complex layout")

            decision = choose_smart_route(pdf, engine_choice="auto")

        self.assertEqual(decision.effective_engine, "vl")
        self.assertEqual(decision.route_reason, "pdf_complex_layout_prefers_vl")
        self.assertIn("complex_keyword:formula", decision.signals)
        self.assertIn("complex_keyword:table", decision.signals)

    def test_explicit_engine_is_preserved(self) -> None:
        from localocr.smart_router import choose_smart_route

        decision = choose_smart_route(Path("tests/samples/probe_text.png"), engine_choice="vl")

        self.assertEqual(decision.effective_engine, "vl")
        self.assertEqual(decision.route_reason, "explicit_vl")
        self.assertIn("explicit_engine", decision.signals)

    def test_explicit_model_is_explained_but_not_rerouted(self) -> None:
        from localocr.model_registry import select_model_profile_with_route

        profile, decision = select_model_profile_with_route(
            Path("tests/samples/probe_text.png"),
            engine_choice="auto",
            model_choice="paddleocr-vl-1.6",
        )

        self.assertEqual(profile.id, "paddleocr-vl-1.6")
        self.assertEqual(decision.effective_engine, "vl")
        self.assertEqual(decision.model_id, "paddleocr-vl-1.6")
        self.assertEqual(decision.route_reason, "explicit_model")
        self.assertIn("explicit_model", decision.signals)

    def test_structure_is_not_selected_by_auto(self) -> None:
        from localocr.smart_router import choose_smart_route

        decision = choose_smart_route(Path("tests/samples/table_with_seal.png"), engine_choice="auto")

        self.assertNotEqual(decision.effective_engine, "structure")


if __name__ == "__main__":
    unittest.main()
