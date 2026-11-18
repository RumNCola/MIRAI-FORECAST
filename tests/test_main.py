"""Application orchestration and Windows launcher behavior without network access."""

import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from unittest.mock import Mock, patch

import main as application
from settings.config import INVENTORY_PARAMETERS_WORKBOOK, INVENTORY_SCRIPT, MODEL_SCRIPTS
from src.runtime import PipelineError, find_rscript, run_logged


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for relative in (*MODEL_SCRIPTS.values(), INVENTORY_SCRIPT, INVENTORY_PARAMETERS_WORKBOOK):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture", encoding="utf-8")
        self.find = self.enterContext(patch("main.find_rscript", return_value=self.root / "Rscript.exe"))
        self.enterContext(patch("main.r_environment", return_value={"R_LIBS_USER": "fixture-library"}))
        self.child = self.enterContext(patch("main.run_logged"))
        self.elt = self.enterContext(patch("src.elt.ELT"))
        self.elt.return_value.run.return_value = Mock(processed=1, skipped=2, failed=0)
        self.enterContext(patch("sys.stderr", new_callable=io.StringIO))

    def events(self):
        return (self.root / "pipeline.log").read_text(encoding="utf-8")

    def test_elt_runs_before_franklin_and_mut(self):
        order = []
        self.child.side_effect = lambda *args, **kwargs: order.append(kwargs["label"])
        def elt_result():
            order.append("ELT")
            return Mock(processed=0, skipped=3, failed=0)
        self.elt.return_value.run.side_effect = elt_result
        self.assertEqual(application.main(self.root), 0)
        self.assertEqual(order, ["R dependency check", "ELT", "FRANKLIN demand model", "MUT demand model",
                                 "Inventory model"])
        self.assertIn("Analysis pipeline completed successfully", self.events())
        self.assertFalse((self.root / ".pipeline.lock").exists())

    def test_recoverable_elt_rejections_allow_both_models(self):
        self.elt.return_value.run.return_value = Mock(processed=1, skipped=0, failed=1)
        self.assertEqual(application.main(self.root), 0)
        self.assertEqual(self.child.call_count, 4)
        self.assertIn("WARNING", self.events())
        self.assertNotIn("ERROR", self.events())

    def test_fatal_elt_failure_stops_before_any_model(self):
        self.elt.return_value.run.side_effect = RuntimeError("ELT failed")
        self.assertEqual(application.main(self.root), 1)
        self.assertEqual(self.child.call_count, 1)
        self.assertIn("ERROR", self.events())
        self.assertNotIn("ELT completed:", self.events())
        self.assertNotIn("Analysis pipeline completed successfully", self.events())

    def test_franklin_failure_prevents_mut_and_final_success(self):
        self.child.side_effect = [None, PipelineError("Franklin failed")]
        self.assertEqual(application.main(self.root), 1)
        self.assertEqual(self.child.call_count, 2)
        self.assertNotIn("FRANKLIN analysis saved", self.events())
        self.assertNotIn("MUT analysis saved", self.events())
        self.assertIn("ERROR", self.events())

    def test_missing_r_fails_without_modifying_raw(self):
        self.find.return_value = None
        self.assertEqual(application.main(self.root), 1)
        self.elt.assert_not_called()
        self.assertIn("Run main.bat", self.events())

    def test_inventory_runs_last_with_its_own_configuration(self):
        self.assertEqual(application.main(self.root), 0)
        inventory_call = self.child.call_args_list[-1]
        self.assertEqual(inventory_call.kwargs["label"], "Inventory model")
        environment = inventory_call.kwargs["env"]
        self.assertNotIn("MIRAI_MODEL_CONFIG", environment)
        config = __import__("json").loads(Path(environment["MIRAI_INVENTORY_CONFIG"]).read_text(encoding="utf-8"))
        self.assertEqual([site["site"] for site in config["sites"]], ["FRANKLIN", "MUT"])
        self.assertEqual(config["sites"][1]["delivery_weekdays"], [2, 5])
        self.assertIn("Inventory analysis saved", self.events())

    def test_saved_defaults_drive_console_runs(self):
        defaults = self.root / "settings" / "valores_por_defecto.json"
        defaults.write_text('{"schema_version": 1, "general": {"horizon_days": 14}, '
                            '"sites": {"MUT": {"delivery_weekdays": [3]}}}', encoding="utf-8")
        self.assertEqual(application.main(self.root), 0)
        environment = self.child.call_args_list[-1].kwargs["env"]
        config = __import__("json").loads(Path(environment["MIRAI_INVENTORY_CONFIG"]).read_text(encoding="utf-8"))
        self.assertEqual(config["sites"][1]["delivery_weekdays"], [3])
        self.assertEqual(config["sites"][1]["horizon_days"], 14)
        self.assertIn("Using saved defaults", self.events())
        defaults.write_text("{broken", encoding="utf-8")
        self.assertEqual(application.main(self.root), 1)
        self.assertIn("valores_por_defecto.json is invalid", self.events())

    def test_missing_parameters_workbook_fails_before_any_child_process(self):
        (self.root / INVENTORY_PARAMETERS_WORKBOOK).unlink()
        self.assertEqual(application.main(self.root), 1)
        self.child.assert_not_called()
        self.assertIn("inventory parameters workbook", self.events())

    def test_existing_pipeline_lock_is_not_deleted(self):
        path = self.root / ".pipeline.lock"
        path.write_text("another process", encoding="utf-8")
        self.assertEqual(application.main(self.root), 1)
        self.assertEqual(path.read_text(), "another process")
        self.child.assert_not_called()

    def test_keyboard_interrupt_returns_failure_and_releases_lock(self):
        self.elt.return_value.run.side_effect = KeyboardInterrupt()
        self.assertEqual(application.main(self.root), 1)
        self.assertFalse((self.root / ".pipeline.lock").exists())


class RuntimeTests(unittest.TestCase):
    def test_child_warnings_are_logged_and_success_follows_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            stream = io.StringIO()
            logger = logging.Logger("child-test", level=logging.INFO)
            handler = logging.StreamHandler(stream)
            handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
            logger.addHandler(handler)
            run_logged([sys.executable, "-c", "print('Warning: recoverable')"], label="child",
                       root=Path(directory), logger=logger)
            self.assertIn("WARNING", stream.getvalue())
            self.assertTrue(stream.getvalue().rstrip().endswith("child completed"))

    def test_nonzero_child_exit_is_fatal_without_completion_message(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = Mock()
            with self.assertRaisesRegex(PipelineError, "exit code 7"):
                run_logged([sys.executable, "-c", "raise SystemExit(7)"], label="child",
                           root=Path(directory), logger=logger)
            logger.info.assert_not_called()

    def test_r_startup_warnings_and_numbered_details_keep_warning_severity(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = Mock()
            program = "print('During startup - Warning messages:'); print('1: recoverable detail'); print('INFO: ready')"
            run_logged([sys.executable, "-c", program], label="R", root=Path(directory), logger=logger)
            self.assertEqual([call.args[0] for call in logger.log.call_args_list],
                             [logging.WARNING, logging.WARNING, logging.INFO])

    def test_invalid_explicit_rscript_does_not_fall_back_silently(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MIRAI_RSCRIPT": "missing-Rscript.exe"}):
            with self.assertRaises(PipelineError):
                find_rscript(Path(directory))

    @unittest.skipUnless(os.name == "nt", "Windows batch launcher")
    def test_batch_runs_from_another_directory_and_preserves_application_exit_code(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repository with spaces"
            root.mkdir()
            shutil.copyfile(Path(__file__).resolve().parents[1] / "main.bat", root / "main.bat")
            venv.EnvBuilder(with_pip=False).create(root / ".venv")
            (root / "scripts").mkdir()
            (root / "scripts" / "bootstrap.py").write_text("raise SystemExit(0)", encoding="utf-8")
            (root / "main.py").write_text("from pathlib import Path\nPath('called').write_text('yes')\nraise SystemExit(7)", encoding="utf-8")
            result = subprocess.run(["cmd.exe", "/d", "/c", str(root / "main.bat")], cwd=directory,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertTrue((root / "called").exists())
            (root / "called").unlink()
            (root / "scripts" / "bootstrap.py").write_text("raise SystemExit(1)", encoding="utf-8")
            result = subprocess.run(["cmd.exe", "/d", "/c", str(root / "main.bat")], cwd=directory,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertFalse((root / "called").exists())
