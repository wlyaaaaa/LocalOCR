from __future__ import annotations

import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class ModelRegistryTest(unittest.TestCase):
    def test_default_profiles_are_loaded_from_data_file(self) -> None:
        from localocr.model_registry import load_model_profiles

        registry = load_model_profiles()

        self.assertEqual(registry.defaults["ocr"], "ppocrv6-medium")
        self.assertEqual(registry.defaults["vl"], "paddleocr-vl-1.6")
        self.assertIn("plain_ocr", registry.profiles["ppocrv6-medium"].capabilities)
        self.assertIn("layout_vl", registry.profiles["paddleocr-vl-1.6"].capabilities)

    def test_engine_aliases_resolve_to_default_profile_ids(self) -> None:
        from localocr.model_registry import resolve_model_reference

        self.assertEqual(resolve_model_reference("ocr").id, "ppocrv6-medium")
        self.assertEqual(resolve_model_reference("vl").id, "paddleocr-vl-1.6")
        self.assertEqual(resolve_model_reference("ppocrv6-medium").engine, "ocr")

    def test_model_choice_can_override_auto_route(self) -> None:
        from localocr.model_registry import select_model_profile

        profile = select_model_profile(
            Path("tests/samples/probe_text.png"),
            engine_choice="auto",
            model_choice="paddleocr-vl-1.6",
        )

        self.assertEqual(profile.id, "paddleocr-vl-1.6")
        self.assertEqual(profile.engine, "vl")

    def test_conflicting_engine_and_model_choice_is_rejected(self) -> None:
        from localocr.model_registry import select_model_profile

        with self.assertRaisesRegex(ValueError, "does not match engine"):
            select_model_profile(
                Path("tests/samples/probe_text.png"),
                engine_choice="ocr",
                model_choice="paddleocr-vl-1.6",
            )


if __name__ == "__main__":
    unittest.main()
