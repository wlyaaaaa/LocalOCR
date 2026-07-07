from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class RouterCompatibilityTest(unittest.TestCase):
    def test_route_engine_auto_matches_smart_router_plain_pdf_default(self) -> None:
        from localocr.router import route_engine

        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "address-confirmation.pdf"
            pdf.write_bytes(b"%PDF-1.7 plain scan")

            self.assertEqual(route_engine(pdf, "auto"), "ocr")

    def test_route_engine_auto_matches_smart_router_complex_pdf_name(self) -> None:
        from localocr.router import route_engine

        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "lecture-formula-table-layout.pdf"
            pdf.write_bytes(b"%PDF-1.7 complex layout")

            self.assertEqual(route_engine(pdf, "auto"), "vl")

    def test_route_engine_preserves_explicit_override(self) -> None:
        from localocr.router import route_engine

        self.assertEqual(route_engine(Path("anything.png"), "structure"), "structure")


if __name__ == "__main__":
    unittest.main()
