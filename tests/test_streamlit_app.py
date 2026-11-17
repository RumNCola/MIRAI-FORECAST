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
            self.assertEqual(len(app.file_uploader), 2)

    def test_upload_execute_and_render_forecast(self):
        from streamlit.testing.v1 import AppTest
        import polars as pl
        from src.interface import create_run as real_create_run
        stream = io.BytesIO()
        workbook = Workbook()
        workbook.active.title = "Sheet1"
        workbook.save(stream)

        def fake_model(root, run, settings, handler):
            output = run / "out" / "mut"
            output.mkdir(parents=True)
            (output / "config_used.json").write_text(json.dumps({"horizon_days": settings["MUT"].horizon_days, "model": "dia"}))
            (output / "modelo_demanda_MUT.xlsx").write_bytes(stream.getvalue())
            pl.DataFrame({"producto": ["Ramen"], "fecha": ["2026-11-18"], "demanda_predicha": [12.0]}).write_parquet(output / "pronosticos.parquet")

        with tempfile.TemporaryDirectory() as directory, \
             patch("src.runtime.find_rscript", return_value=Path("Rscript.exe")), \
             patch("src.interface.create_run", side_effect=lambda root, uploads, settings: real_create_run(Path(directory), uploads, settings)), \
             patch("src.interface.execute_run", side_effect=fake_model) as execute:
            app = AppTest.from_file(str(APP), default_timeout=30).run()
            app.file_uploader[0].set_value(("ventas.xlsx", stream.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")).run()
            self.assertFalse(app.exception)
            self.assertFalse(app.button[0].disabled)
            app.button[0].click().run()
            self.assertFalse(app.exception)
            execute.assert_called_once()
            self.assertEqual(app.metric[0].value, "1")
            self.assertEqual(len(app.get("download_button")), 2)
