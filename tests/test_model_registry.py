from __future__ import annotations

import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class ModelRegistryTest(unittest.TestCase):
    def test_default_profiles_are_loaded_from_data_file(self) -> None:
        from localocr.model_registry import load_model_profiles

        registry = load_model_profiles()

        self.assertEqual(registry.defaults["ocr"], "ppocrv6-medium")
        self.assertEqual(registry.defaults["vl"], "paddleocr-vl-1.6")
        self.assertEqual(registry.defaults["structure"], "pp-structure-v3")
        self.assertIn("plain_ocr", registry.profiles["ppocrv6-medium"].capabilities)
        self.assertIn("layout_vl", registry.profiles["paddleocr-vl-1.6"].capabilities)
        self.assertIn("layout_structure", registry.profiles["pp-structure-v3"].capabilities)
        self.assertEqual(registry.profiles["paddleocr-vl-1.6"].options["cuda_module_loading"], "EAGER")
        self.assertFalse(registry.profiles["ppocrv6-medium"].options["use_doc_unwarping"])

    def test_engine_aliases_resolve_to_default_profile_ids(self) -> None:
        from localocr.model_registry import resolve_model_reference

        self.assertEqual(resolve_model_reference("ocr").id, "ppocrv6-medium")
        self.assertEqual(resolve_model_reference("vl").id, "paddleocr-vl-1.6")
        self.assertEqual(resolve_model_reference("structure").id, "pp-structure-v3")
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

    def test_structure_profile_can_be_selected_explicitly(self) -> None:
        from localocr.model_registry import select_model_profile

        profile = select_model_profile(
            Path("tests/samples/sample_table.png"),
            engine_choice="structure",
            model_choice=None,
        )

        self.assertEqual(profile.id, "pp-structure-v3")
        self.assertEqual(profile.engine, "structure")

    def test_conflicting_engine_and_model_choice_is_rejected(self) -> None:
        from localocr.model_registry import select_model_profile

        with self.assertRaisesRegex(ValueError, "does not match engine"):
            select_model_profile(
                Path("tests/samples/probe_text.png"),
                engine_choice="ocr",
                model_choice="paddleocr-vl-1.6",
            )


    def test_worker_option_is_hashed_in_profile_but_not_forwarded_to_paddle_constructor(self):
        from localocr.model_registry import get_engine, resolve_model_reference

        with patch("localocr.model_registry.importlib.import_module",
                   return_value=SimpleNamespace(VLEngine=lambda **kwargs: kwargs)):
            actual = get_engine("vl")
        self.assertNotIn("cuda_module_loading", actual["options"])
        self.assertEqual(resolve_model_reference("vl").options["cuda_module_loading"], "EAGER")

    def test_cuda_loading_mode_is_applied_after_lease_before_paddle_probe(self):
        from localocr.runtime import _predict

        events = []

        def probe():
            self.assertEqual(os.environ.get("CUDA_MODULE_LOADING"), "EAGER")
            events.append("probe")
            return {}

        def engine(*_args, **_kwargs):
            self.assertEqual(os.environ.get("CUDA_MODULE_LOADING"), "EAGER")
            events.append("engine")
            return SimpleNamespace(engine_name="Fake", model_name="Fake", predict_image=lambda _: {"pages": [{"blocks": []}]})

        fake_probe = SimpleNamespace(probe_gpu=probe, format_probe=lambda _: "fake GPU")
        with patch.dict(os.environ, {"CUDA_MODULE_LOADING": "LAZY"}), \
             patch.dict(sys.modules, {"localocr.gpu_probe": fake_probe}), \
             patch("localocr.gpu_broker.verify_inherited_gpu_lease", side_effect=lambda _: events.append("lease")), \
             patch("localocr.model_registry.get_engine", side_effect=engine):
            result = _predict({"device": "gpu:0", "profile_id": "paddleocr-vl-1.6",
                               "probe_gpu": True, "lease": {}, "path": str(Path(__file__).parent / "samples/probe_text.png"), "tmp_dir": os.environ.get("TEMP", "/tmp")}, {}, lambda _: None)
        self.assertEqual(events, ["lease", "probe", "engine"])
        self.assertEqual(result["cuda_module_loading"], "EAGER")

    def test_explicit_cpu_never_probes_gpu_or_alters_cuda_environment(self):
        from localocr.runtime import _predict

        fake_engine = SimpleNamespace(engine_name="Fake", model_name="Fake", predict_image=lambda _: {"pages": [{"blocks": []}]})
        with patch.dict(os.environ, {"CUDA_MODULE_LOADING": "LAZY"}), \
             patch("localocr.gpu_broker.verify_inherited_gpu_lease") as lease, \
             patch("localocr.model_registry.get_engine", return_value=fake_engine):
            result = _predict({"device": "cpu", "profile_id": "paddleocr-vl-1.6",
                               "probe_gpu": True, "path": str(Path(__file__).parent / "samples/probe_text.png"), "tmp_dir": os.environ.get("TEMP", "/tmp")}, {}, lambda _: None)
            self.assertEqual(os.environ["CUDA_MODULE_LOADING"], "LAZY")
        lease.assert_not_called()
        self.assertNotIn("cuda_module_loading", result)


if __name__ == "__main__":
    unittest.main()
