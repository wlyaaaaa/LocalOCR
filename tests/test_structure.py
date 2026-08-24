from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localocr.engines.ppocrv6 import PPOCRv6Engine
from localocr.engines.structure import StructureV3Engine
from localocr.engines.vl import VLEngine
from localocr.pdf_utils import rendered_pdf_page_metadata, read_png_dimensions
from localocr.service import OCRService


class FakePredictor:
    def __init__(self, result: dict) -> None:
        self.item = SimpleNamespace(json={"res": result})

    def predict(self, _image_path: str):
        return [self.item]


class StructureAdapterTest(unittest.TestCase):
    def test_structure_retains_details_lines_and_excluded_regions(self) -> None:
        data = {
            "width": 1000,
            "height": 800,
            "parsing_res_list": [
                {
                    "block_label": "table",
                    "block_content": "<table><tr><td>A</td></tr></table>",
                    "block_bbox": [1, 2, 30, 40],
                    "block_polygon_points": [[1, 2], [30, 2], [30, 40], [1, 40]],
                    "block_order": 0,
                },
                {
                    "block_label": "person",
                    "block_content": "person description",
                    "block_bbox": [100, 100, 200, 200],
                    "block_order": 1,
                },
                {
                    "block_label": "seal",
                    "block_content": "seal text",
                    "block_bbox": [50, 60, 80, 90],
                    "block_order": 2,
                },
            ],
            "table_res_list": [{"cell_box_list": [[1, 2, 3, 4]], "score": 0.95}],
            "formula_res_list": [{"formula": "x^2", "score": 0.88}],
            "seal_res_list": [{"bbox": [50, 60, 80, 90], "score": 0.91}],
            "region_det_res_list": [{"label": "figure", "bbox": [100, 100, 200, 200]}],
            "overall_ocr_res": {
                "rec_texts": ["raw line"],
                "rec_scores": [0.87],
                "rec_boxes": [[5, 6, 40, 20]],
                "dt_polys": [[[5, 6], [40, 6], [40, 20], [5, 20]]],
            },
        }
        engine = StructureV3Engine(device="cpu")
        engine._ensure = lambda: FakePredictor(data)

        result = engine.predict_image("synthetic.png")
        page = result["pages"][0]

        self.assertEqual(page["coordinate_space"], "image_pixels")
        self.assertEqual(page["structure_details"]["table_res_list"][0]["cell_box_list"], [[1, 2, 3, 4]])
        self.assertEqual(page["structure_details"]["formula_res_list"][0]["score"], 0.88)
        self.assertEqual(page["structure_details"]["seal_res_list"][0]["bbox"], [50, 60, 80, 90])
        self.assertEqual(page["text_lines"][0]["text"], "raw line")
        self.assertEqual(page["text_lines"][0]["rect"], [5, 6, 40, 20])
        self.assertEqual(page["blocks"][0]["rect"], [1, 2, 30, 40])
        self.assertEqual([region["label"] for region in page["excluded_regions"]], ["person"])
        self.assertNotIn("person description", [block["text"] for block in page["blocks"]])
        self.assertNotIn("parsing_res_list", page["structure_details"])
        self.assertNotIn("overall_ocr_res", page["structure_details"])
        self.assertNotIn("person description", json.dumps(result, ensure_ascii=False))
        json.dumps(result, ensure_ascii=False)

    def test_ocr_and_vl_adapters_add_rect_without_changing_bbox(self) -> None:
        ocr = PPOCRv6Engine(device="cpu")
        ocr._ensure = lambda: FakePredictor(
            {
                "dt_polys": [[[1, 2], [10, 2], [10, 20], [1, 20]]],
                "rec_texts": ["ocr"],
                "rec_scores": [0.9],
                "rec_boxes": [[1, 2, 10, 20]],
            }
        )
        ocr_block = ocr.predict_image("synthetic.png")["pages"][0]["blocks"][0]
        self.assertEqual(ocr_block["bbox"], [[1, 2], [10, 2], [10, 20], [1, 20]])
        self.assertEqual(ocr_block["rect"], [1, 2, 10, 20])
        self.assertEqual(ocr_block["polygon"], ocr_block["bbox"])

        vl = VLEngine(device="cpu")
        vl._ensure = lambda: FakePredictor(
            {
                "parsing_res_list": [
                    {
                        "block_label": "text",
                        "block_content": "vl",
                        "block_bbox": [1, 2, 10, 20],
                        "block_polygon_points": [[1, 2], [10, 2], [10, 20], [1, 20]],
                    },
                    {
                        "block_label": "face",
                        "block_content": "identity guess",
                        "block_bbox": [20, 20, 40, 40],
                    }
                ]
            }
        )
        vl_page = vl.predict_image("synthetic.png")["pages"][0]
        vl_block = vl_page["blocks"][0]
        self.assertEqual(vl_block["bbox"], [1, 2, 10, 20])
        self.assertEqual(vl_block["rect"], [1, 2, 10, 20])
        self.assertEqual(vl_block["coordinate_space"], "image_pixels")
        self.assertEqual([region["label"] for region in vl_page["excluded_regions"]], ["face"])
        self.assertNotIn("identity guess", [block["text"] for block in vl_page["blocks"]])
        self.assertNotIn("identity guess", json.dumps(vl_page, ensure_ascii=False))

    def test_nonempty_parsing_does_not_reintroduce_excluded_lines_as_blocks(self) -> None:
        data = {
            "parsing_res_list": [
                {
                    "block_label": "person",
                    "block_content": "description",
                    "block_bbox": [1, 2, 30, 40],
                }
            ],
            "overall_ocr_res": {
                "rec_texts": ["raw line"],
                "rec_boxes": [[5, 6, 40, 20]],
            },
        }
        engine = StructureV3Engine(device="cpu")
        engine._ensure = lambda: FakePredictor(data)

        page = engine.predict_image("synthetic.png")["pages"][0]

        self.assertEqual(page["blocks"], [])
        self.assertEqual(page["text_lines"][0]["text"], "raw line")
        self.assertEqual(page["excluded_regions"][0]["label"], "person")


class PdfCoordinateContractTest(unittest.TestCase):
    def test_rendered_pdf_metadata_is_pixel_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 640, 480))
            self.assertEqual(read_png_dimensions(path), (640, 480))
            metadata = rendered_pdf_page_metadata(path)
            self.assertTrue(metadata["rendered_pdf_pixels"])
            self.assertEqual(metadata["render_scale"], 2.0)
            self.assertEqual(metadata["rendered_width"], 640)
            self.assertEqual(metadata["rendered_height"], 480)

    def test_service_adds_pdf_metadata_without_model_inference(self) -> None:
        result = {"pages": [{"page_index": 0, "width": 640, "height": 480}]}
        OCRService._annotate_pdf_pages(result, [])
        page = result["pages"][0]
        self.assertTrue(page["rendered_pdf_pixels"])
        self.assertEqual(page["render_scale"], 2.0)
        self.assertEqual(page["rendered_width"], 640)
        self.assertEqual(page["rendered_height"], 480)
        self.assertEqual(page["coordinate_space"], "image_pixels")


if __name__ == "__main__":
    unittest.main()
