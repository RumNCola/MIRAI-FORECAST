"""Configuration tests plus native R integration when R/packages are available."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from settings.config import MODEL_SETTINGS, get_model_config
from src.elt import ELT
from src.runtime import find_rscript, r_environment


REPOSITORY = Path(__file__).resolve().parents[1]


class ModelConfigTests(unittest.TestCase):
    def test_site_defaults_use_distinct_sales_sources_and_output_directories(self):
        franklin, mut = get_model_config("FRANKLIN"), get_model_config("MUT")
        self.assertEqual(franklin["source"], "FRANKLIN_SALES")
        self.assertEqual(mut["source"], "MUT_SALES")
        self.assertEqual(franklin["model"], "dia_mes_anual")
        self.assertEqual(mut["model"], "dia")
        self.assertEqual(franklin["operating_weekdays"], (5, 6, 7))
        self.assertEqual(mut["operating_weekdays"], (1, 2, 3, 4, 5, 6, 7))
        self.assertEqual(Path(franklin["output_directory"]), REPOSITORY / "out" / "franklin")
        self.assertEqual(Path(mut["output_directory"]), REPOSITORY / "out" / "mut")

    def test_future_ui_can_supply_settings_without_modifying_module_defaults(self):
        settings = replace(MODEL_SETTINGS["MUT"], horizon_days=14, model="dia_mes_anual",
                           output_subdirectory="mut/scenario", generate_plots=False)
        config = get_model_config("MUT", settings=settings)
        self.assertEqual(config["horizon_days"], 14)
        self.assertEqual(config["model"], "dia_mes_anual")
        self.assertFalse(config["generate_plots"])
        self.assertEqual(get_model_config("MUT")["horizon_days"], 28)

    def test_invalid_settings_fail_before_subprocess_or_file_io(self):
        for changes in ({"horizon_days": 0}, {"horizon_days": True}, {"horizon_days": 1.5},
                        {"model": "unknown"}, {"operating_weekdays": ()},
                        {"operating_weekdays": (0, 8)}, {"operating_weekdays": (1, 1)},
                        {"export_excel": False, "export_parquet": False},
                        {"residual_min_observations": 1}, {"generate_plots": "yes"},
                        {"output_subdirectory": "../private"}, {"output_subdirectory": "."},
                        {"source": "FRANKLIN_STORE"}, {"product_column": ""},
                        {"product_column": "date"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                get_model_config("FRANKLIN", settings=replace(MODEL_SETTINGS["FRANKLIN"], **changes))

    def test_mut_store_alias_can_be_selected_explicitly(self):
        config = get_model_config("MUT", settings=replace(MODEL_SETTINGS["MUT"], source="MUT_STORE"))
        self.assertEqual(config["source"], "MUT_STORE")
        self.assertTrue(config["input_directory"].endswith("/MUT_STORE"))

    def test_optional_excel_dependency_tracks_enabled_formats(self):
        config = get_model_config("MUT", settings=replace(MODEL_SETTINGS["MUT"], export_excel=False))
        self.assertNotIn("writexl", config["required_r_packages"])
        self.assertIn("arrow", config["required_r_packages"])

    def test_cli_emits_valid_json_independent_of_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run([sys.executable, str(REPOSITORY / "settings" / "config.py"),
                                        "--site", "MUT"], cwd=directory, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        config = json.loads(completed.stdout)
        self.assertEqual(config["site"], "MUT")
        self.assertEqual(config["repository_root"], REPOSITORY.as_posix())
        self.assertEqual(config["horizon_days"], 28)

    def test_import_does_not_start_the_pipeline_or_create_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            program = f"import sys; sys.path.insert(0, {str(REPOSITORY)!r}); import settings.config"
            completed = subprocess.run([sys.executable, "-c", program], cwd=directory,
                                       capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])

    @unittest.skipUnless(importlib.util.find_spec("tree_sitter_languages"), "Optional R syntax checker is unavailable")
    def test_r_sources_have_valid_syntax(self):
        from tree_sitter_languages import get_parser
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            parser = get_parser("r")
        for path in (REPOSITORY / "scripts").glob("*.R"):
            with self.subTest(path=path.name):
                self.assertFalse(parser.parse(path.read_bytes()).root_node.has_error)


class RModelIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rscript = find_rscript(REPOSITORY)
        if not cls.rscript:
            raise unittest.SkipTest("Rscript is not installed; native R model execution cannot be checked")
        cls.r_environment = r_environment(REPOSITORY, cls.rscript)
        check = subprocess.run([str(cls.rscript), "--vanilla", "-e", (
            "packages <- c('arrow', 'digest', 'jsonlite', 'tzdb', 'writexl'); "
            "quit(status=if(all(vapply(packages, requireNamespace, logical(1), quietly=TRUE))) 0 else 1)"
        )], env=cls.r_environment, capture_output=True, text=True)
        if check.returncode:
            raise unittest.SkipTest("R model packages are missing: arrow, digest, jsonlite, tzdb, writexl")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repository with spaces"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "settings").mkdir()
        for path in (REPOSITORY / "scripts").glob("*.R"):
            shutil.copyfile(path, self.root / "scripts" / path.name)
        config = (REPOSITORY / "settings" / "config.py").read_text(encoding="utf-8")
        config = config.replace("HORIZON_DAYS = 28", "HORIZON_DAYS = 7")
        config = config.replace("generate_plots: bool = True", "generate_plots: bool = False")
        (self.root / "settings" / "config.py").write_text(config, encoding="utf-8")
        rows = []
        for day in range(28):
            date = datetime(2026, 7, 6) + timedelta(days=day)
            for number, name in enumerate(("Product A", "Producto ñ "), 1):
                rows.append({"date": date, "category": "Food", "subcategory": "Lunch",
                             "product_name": name, "quantity": number + day % 4,
                             "order_reference": f"order-{day}", "order_id": str(day),
                             "product_sku": str(number), "status": "pagado"})
        self.rows = pl.DataFrame(rows)
        self.raw = self.root / "Data" / "RAW"
        for source in ("FRANKLIN_SALES", "MUT_SALES"):
            (self.raw / source).mkdir(parents=True)
        self.rows.select("date", "category", "subcategory", "product_name", "quantity").write_parquet(
            self.raw / "FRANKLIN_SALES" / "daily.parquet")
        self.rows.head(28).write_parquet(self.raw / "MUT_SALES" / "first.parquet")
        self.rows.tail(28).with_columns(pl.lit(12500).alias("unit_price")).write_parquet(
            self.raw / "MUT_SALES" / "second.parquet")
        shutil.copyfile(self.raw / "MUT_SALES" / "first.parquet", self.raw / "MUT_SALES" / "copy.parquet")
        result = ELT(self.root).run()
        self.assertEqual(result.failed, 0)
        self.environment = {**self.r_environment, "MIRAI_PYTHON": sys.executable}

    def run_model(self, site):
        script = "modelo_demanda_Franklin.R" if site == "FRANKLIN" else "modelo_demanda_MUT.R"
        return subprocess.run([str(self.rscript), "--vanilla", str(self.root / "scripts" / script)], cwd=self.root.parent,
                              env=self.environment, capture_output=True, text=True)

    def test_models_read_del_and_write_configured_analysis_outputs(self):
        # Untracked DEL files must not enter a model, even when they look like exports.
        self.rows.write_parquet(self.root / "Data" / "DEL" / "MUT_SALES" / "untracked.parquet")
        for site, directory in (("FRANKLIN", "franklin"), ("MUT", "mut")):
            with self.subTest(site=site):
                completed = self.run_model(site)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                output = self.root / "out" / directory
                metrics = pl.read_parquet(output / "metricas.parquet")
                forecast = pl.read_parquet(output / "pronosticos.parquet")
                expected = self.rows
                if site == "FRANKLIN":
                    expected = expected.filter(pl.col("date").dt.weekday().is_in([5, 6, 7]))
                self.assertEqual(metrics["unidades_vendidas"].sum(), expected["quantity"].sum())
                self.assertEqual(set(metrics["producto"]), {"Product A", "Producto ñ "})
                self.assertEqual(forecast["fecha"].n_unique(), 3 if site == "FRANKLIN" else 7)
                self.assertEqual(len(list(output.glob("*.parquet"))), 6)
                self.assertEqual(len(list(output.glob("*.xlsx"))), 1)
                self.assertFalse((output / "graphs").exists())
                snapshot = json.loads((output / "config_used.json").read_text(encoding="utf-8"))
                self.assertEqual(snapshot["horizon_days"], 7)

    def test_changed_del_artifact_is_rejected_before_model_outputs(self):
        artifact = next((self.root / "Data" / "DEL" / "MUT_SALES").glob("*.parquet"))
        artifact.write_bytes(b"changed artifact")
        completed = self.run_model("MUT")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("changed after ELT", completed.stderr)
        self.assertFalse((self.root / "out").exists())


if __name__ == "__main__":
    unittest.main()
