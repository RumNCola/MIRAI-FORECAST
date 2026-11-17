"""Upload isolation and the UI-to-model configuration contract."""
import io
import json
import logging
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from settings.config import MODEL_SETTINGS
from src.interface import create_run, result_zip, validate_uploads
from main import run_pipeline


def excel_bytes():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", "fixture")
    return stream.getvalue()


class InterfaceTests(unittest.TestCase):
    def test_runs_preserve_both_files_with_same_name_and_isolate_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = {"MUT": MODEL_SETTINGS["MUT"]}
            uploads = {"MUT": [("../../sales.xlsx", excel_bytes()), ("sales.xlsx", excel_bytes() + b"different")]}
            first = create_run(root, uploads, settings)
            second = create_run(root, uploads, settings)
            self.assertNotEqual(first, second)
            paths = list((first / "Data" / "RAW" / "MUT_SALES").rglob("*.xlsx"))
            self.assertEqual(len(paths), 2)
            self.assertTrue(all(p.resolve().is_relative_to(first) for p in paths))
            self.assertFalse((root / "Data").exists())

    def test_reject_empty_invalid_and_duplicate_uploads(self):
        settings = {"MUT": MODEL_SETTINGS["MUT"]}
        for files in ([], [("a.xls", b"x")], [("a.xlsx", b"broken")],
                      [("a.xlsx", excel_bytes()), ("b.xlsx", excel_bytes())]):
            with self.subTest(files=files), self.assertRaises(ValueError):
                validate_uploads({"MUT": files}, settings)

    def test_zip_contains_only_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "out" / "mut").mkdir(parents=True)
            (root / "out" / "mut" / "report.xlsx").write_bytes(b"report")
            (root / "private.xlsx").write_bytes(b"private")
            with zipfile.ZipFile(io.BytesIO(result_zip(root))) as archive:
                self.assertEqual(archive.namelist(), ["mut/report.xlsx"])

    def test_selected_site_receives_custom_config_and_isolated_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "modelo_demanda_MUT.R").touch()
            run = root / "session"
            run.mkdir()
            settings = {"MUT": replace(MODEL_SETTINGS["MUT"], horizon_days=14, operating_weekdays=(1, 2))}
            with patch("main.find_rscript", return_value=root / "Rscript.exe"), \
                 patch("main.r_environment", return_value={"R_LIBS_USER": "library"}), \
                 patch("main.run_logged") as child, patch("src.elt.ELT") as elt:
                elt.return_value.run.return_value = Mock(processed=1, skipped=0, failed=0)
                run_pipeline(root, logging.getLogger("test"), model_settings=settings, data_root=run, strict_inputs=True)
                self.assertEqual(child.call_count, 2)
                elt.assert_called_once_with(run, logger=logging.getLogger("test"))
                config_path = Path(child.call_args.kwargs["env"]["MIRAI_MODEL_CONFIG"])
                config = json.loads(config_path.read_text())
                self.assertEqual(config["horizon_days"], 14)
                self.assertEqual(config["operating_weekdays"], [1, 2])
                self.assertEqual(config["repository_root"], run.as_posix())
                self.assertEqual(MODEL_SETTINGS["MUT"].horizon_days, 28)

    def test_ui_stops_models_when_any_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "modelo_demanda_MUT.R").touch()
            with patch("main.find_rscript", return_value=root / "Rscript.exe"), \
                 patch("main.r_environment", return_value={"R_LIBS_USER": "library"}), \
                 patch("main.run_logged") as child, patch("src.elt.ELT") as elt:
                elt.return_value.run.return_value = Mock(processed=0, skipped=0, failed=1)
                with self.assertRaisesRegex(RuntimeError, "rechazados"):
                    run_pipeline(root, Mock(), model_settings={"MUT": MODEL_SETTINGS["MUT"]}, strict_inputs=True)
                self.assertEqual(child.call_count, 1)
