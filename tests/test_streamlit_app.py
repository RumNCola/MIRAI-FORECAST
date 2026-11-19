"""Exercise the user flow with Streamlit's real widget test runner."""
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"

@unittest.skipUnless(importlib.util.find_spec("streamlit"), "Streamlit is not installed")
class StreamlitTests(unittest.TestCase):
    def test_select_sites_and_validate_missing_opening_days(self):
        from streamlit.testing.v1 import AppTest
        with patch("src.runtime.find_rscript", return_value=Path("Rscript.exe")):
            app = AppTest.from_file(str(APP), default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertTrue(app.button[0].disabled)
            app.multiselect[0].set_value(["MUT", "FRANKLIN"]).run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.file_uploader), 3)  # two sites plus the optional parameters workbook
            self.assertEqual(len(app.multiselect), 5)  # sites, then opening and dispatch days per site

    def test_store_is_offered_as_a_third_location(self):
        from streamlit.testing.v1 import AppTest
        with patch("src.runtime.find_rscript", return_value=Path("Rscript.exe")):
            app = AppTest.from_file(str(APP), default_timeout=30).run()
            self.assertEqual(app.multiselect[0].options, ["Franklin", "MUT", "Tienda Franklin"])
            app.multiselect[0].set_value(["MUT", "FRANKLIN", "FRANKLIN_STORE"]).run()
            self.assertFalse(app.exception)
            self.assertEqual([uploader.label for uploader in app.file_uploader][:3],
                             ["Excel de MUT", "Excel de Franklin", "Excel de Tienda Franklin"])
            self.assertTrue(any("notas de crédito" in caption.value for caption in app.caption))

    def test_production_report_option_and_download(self):
        from streamlit.testing.v1 import AppTest
        from src.interface import create_run as real_create_run
        from src.report import write_status
        stream = io.BytesIO()
        workbook = Workbook()
        workbook.active.title = "Sheet1"
        workbook.save(stream)

        def fake_model(root, run, settings, handler, **options):
            output = run / "out" / "mut"
            output.mkdir(parents=True)
            (output / "config_used.json").write_text(json.dumps({"horizon_days": 28, "model": "dia"}))
            report = run / "out" / "reporte"
            report.mkdir(parents=True)
            (report / "reporte_produccion_2026-11-20.pdf").write_bytes(b"%PDF-1.4 fixture")
            write_status(report, "generado", "Reporte de la semana vie 20/11 – jue 26/11.",
                         "reporte_produccion_2026-11-20.pdf")

        with tempfile.TemporaryDirectory() as directory, \
             patch("src.runtime.find_rscript", return_value=Path("Rscript.exe")), \
             patch("src.interface.create_run", side_effect=lambda root, uploads, settings, parameters=None: real_create_run(Path(directory), uploads, settings, parameters)), \
             patch("src.interface.execute_run", side_effect=fake_model) as execute:
            app = AppTest.from_file(str(APP), default_timeout=30).run()
            self.assertFalse(app.checkbox(key="report-start-today").value)        # off by default
            app.checkbox(key="report-start-today").check().run()
            app.file_uploader[0].set_value(("ventas.xlsx", stream.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")).run()
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertTrue(execute.call_args.kwargs["report_start_today"])
            keys = [button.key for button in app.get("download_button")]
            self.assertIn("download-report", keys)

    def test_upload_execute_and_render_forecast(self):
        from streamlit.testing.v1 import AppTest
        import polars as pl
        from src.interface import create_run as real_create_run
        stream = io.BytesIO()
        workbook = Workbook()
        workbook.active.title = "Sheet1"
        workbook.save(stream)

        def fake_model(root, run, settings, handler, **inventory):
            output = run / "out" / "mut"
            output.mkdir(parents=True)
            (output / "config_used.json").write_text(json.dumps({"horizon_days": settings["MUT"].horizon_days, "model": "dia"}))
            (output / "modelo_demanda_MUT.xlsx").write_bytes(stream.getvalue())
            pl.DataFrame({"producto": ["Ramen"], "fecha": ["2026-11-18"], "demanda_predicha": [12.0]}).write_parquet(output / "pronosticos.parquet")
            inventory = run / "out" / "inventario"
            inventory.mkdir(parents=True)
            (inventory / "modelo_inventario.xlsx").write_bytes(stream.getvalue())
            pl.DataFrame({"local": ["MUT"], "semana_inicio": ["2026-11-16"], "dias_en_horizonte": [7],
                          "ingrediente": ["caldo tonkotsu"], "porciones_esperadas": [120.0], "desviacion_estandar": [10.0],
                          "nivel_confianza": [0.95], "ic_inferior": [100.4], "ic_superior": [139.6],
                          "vida_util_dias": [6.0], "platos_que_lo_usan": ["Tonkotsu"], "porciones_a_preparar": [140]}
                         ).write_parquet(inventory / "resumen_semanal.parquet")

        with tempfile.TemporaryDirectory() as directory, \
             patch("src.runtime.find_rscript", return_value=Path("Rscript.exe")), \
             patch("src.interface.create_run", side_effect=lambda root, uploads, settings, parameters=None: real_create_run(Path(directory), uploads, settings, parameters)), \
             patch("src.interface.execute_run", side_effect=fake_model) as execute:
            app = AppTest.from_file(str(APP), default_timeout=30).run()
            app.file_uploader[0].set_value(("ventas.xlsx", stream.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")).run()
            self.assertFalse(app.exception)
            self.assertFalse(app.button[0].disabled)
            app.button[0].click().run()
            self.assertFalse(app.exception)
            execute.assert_called_once()
            self.assertEqual(execute.call_args.kwargs["inventory_sites"]["MUT"].delivery_weekdays, (2, 5))
            self.assertTrue(execute.call_args.kwargs["inventory"].enabled)
            # Parameters template, zip, inventory workbook and MUT workbook.
            self.assertEqual(len(app.get("download_button")), 4)
            self.assertIn("INVENTARIO", [tab.label for tab in app.tabs])
            metrics = {metric.label: metric.value for metric in app.metric}
            self.assertEqual(metrics["Productos"], "1")
            self.assertEqual(metrics["Porciones esperadas (1.ª semana)"], "120")
            self.assertEqual(metrics["IC 95%"], "100 – 140")

    def test_defaults_view_saves_values_and_replaces_base_after_showing_changes(self):
        from streamlit.testing.v1 import AppTest
        from settings.config import INVENTORY_PARAMETERS_WORKBOOK
        from src.interface import default_parameters_workbook
        from tests.test_defaults import edited_base
        xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        with tempfile.TemporaryDirectory() as directory, \
             patch("src.runtime.find_rscript", return_value=Path("Rscript.exe")), \
             patch("settings.config.REPOSITORY_ROOT", Path(directory)):
            root = Path(directory)
            base = root / INVENTORY_PARAMETERS_WORKBOOK
            base.parent.mkdir(parents=True)
            base.write_bytes(default_parameters_workbook())
            app = AppTest.from_file(str(APP), default_timeout=30).run()
            app.radio(key="view").set_value("Valores por defecto").run()
            self.assertFalse(app.exception)
            # Values inside a form are sent together with the submit button.
            app.multiselect(key="def-delivery-MUT").set_value([1, 4])
            next(b for b in app.button if b.label == "Guardar valores por defecto").click().run()
            self.assertFalse(app.exception)
            saved = json.loads((root / "settings" / "valores_por_defecto.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["sites"]["MUT"]["delivery_weekdays"], [1, 4])
            app.radio(key="view").set_value("Pronóstico").run()
            self.assertEqual(app.multiselect(key="delivery-MUT").value, [1, 4])

            app.radio(key="view").set_value("Valores por defecto").run()
            uploader = next(u for u in app.file_uploader if u.key.startswith("upload-base-parameters"))
            uploader.set_value(("malo.xlsx", b"not an excel", xlsx)).run()
            self.assertTrue(any("Archivo rechazado" in e.value for e in app.error))
            self.assertEqual(base.read_bytes(), default_parameters_workbook())

            new = edited_base(remove_composition=1, add_composition=(("MUT", "Ramen nuevo", "caldo tonkotsu", 1, "x"),
                                                                      ("MUT", "Ramen nuevo", "huevo curado", 1, "x")))
            uploader = next(u for u in app.file_uploader if u.key.startswith("upload-base-parameters"))
            uploader.set_value(("nuevo.xlsx", new, xlsx)).run()
            metrics = {m.label: m.value for m in app.metric}
            self.assertEqual(metrics["composicion: añadidas / eliminadas"], "+2 / −1")
            self.assertEqual(metrics["vida_util: añadidas / eliminadas"], "+0 / −0")
            app.button(key="replace-base-parameters").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(base.read_bytes(), new)
            self.assertEqual(len(list((root / "out" / "parametros_respaldo").glob("*.xlsx"))), 1)
            self.assertTrue(any("Excel base reemplazado" in s.value for s in app.success))

    def test_product_and_ingredient_lists_sort_by_quantity_with_alphabetical_toggle(self):
        from streamlit.testing.v1 import AppTest
        import polars as pl
        with tempfile.TemporaryDirectory() as directory, \
             patch("src.runtime.find_rscript", return_value=Path("Rscript.exe")):
            run = Path(directory)
            output = run / "out" / "mut"
            (output / "graphs").mkdir(parents=True)
            (output / "config_used.json").write_text(json.dumps({"horizon_days": 7, "model": "dia"}))
            pl.DataFrame({"producto": ["agua", "Ramen", "Karaage", "Ramen"],
                          "fecha": ["2026-11-18", "2026-11-18", "2026-11-18", "2026-11-19"],
                          "demanda_predicha": [5.0, 6.0, 8.0, 6.0]}).write_parquet(output / "pronosticos.parquet")
            from PIL import Image
            for name in ("01_agua", "02_Karaage", "03_Ramen"):
                Image.new("RGB", (2, 2)).save(output / "graphs" / f"{name}.png")
            inventory = run / "out" / "inventario"
            inventory.mkdir(parents=True)
            pl.DataFrame({"local": ["MUT"] * 3, "semana_inicio": ["2026-11-16"] * 3, "dias_en_horizonte": [7] * 3,
                          "ingrediente": ["arroz", "caldo tonkotsu", "mochi"], "porciones_esperadas": [10.0, 50.0, 20.0],
                          "desviacion_estandar": [1.0] * 3, "nivel_confianza": [0.95] * 3,
                          "ic_inferior": [8.0, 48.0, 18.0], "ic_superior": [12.0, 52.0, 22.0],
                          "vida_util_dias": [30.0, 6.0, 2.0], "platos_que_lo_usan": ["x"] * 3}
                         ).write_parquet(inventory / "resumen_semanal.parquet")
            app = AppTest.from_file(str(APP), default_timeout=30)
            app.session_state["completed_run"] = (str(run), ["MUT"])
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.selectbox(key="product-MUT").options, ["Ramen", "Karaage", "agua"])
            self.assertEqual(app.selectbox(key="inventory-ingredient").options, ["caldo tonkotsu", "mochi", "arroz"])
            self.assertEqual(app.selectbox(key="graph-MUT").options, ["03_Ramen", "02_Karaage", "01_agua"])
            app.toggle(key="alphabetical-product-MUT").set_value(True).run()
            app.toggle(key="alphabetical-inventory-ingredient").set_value(True).run()
            self.assertEqual(app.selectbox(key="product-MUT").options, ["agua", "Karaage", "Ramen"])
            self.assertEqual(app.selectbox(key="graph-MUT").options, ["01_agua", "02_Karaage", "03_Ramen"])
            self.assertEqual(app.selectbox(key="inventory-ingredient").options, ["arroz", "caldo tonkotsu", "mochi"])
