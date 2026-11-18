"""Production report: period, cycles, 95% production quantities, traffic lights and the PDF."""
import importlib.util
import logging
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import polars as pl
from openpyxl import Workbook

import main as application
from settings.config import REPORT_GROUPS_WORKBOOK, REPOSITORY_ROOT
from src.report import (ReportError, Trend, build_report, cycle_blocks, generate_report, normalize_name,
                        prepare_outputs, production_quantity, read_group_mapping, read_site_outputs, read_status, report_window)

Z95 = 1.6448536269514722
TODAY = date(2026, 11, 18)                         # Wednesday: the report week is Fri 20/11 - Thu 26/11
WEEK = [date(2026, 11, 20) + timedelta(days=i) for i in range(7)]


def write_groups(path: Path, rows) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "grupos"
    sheet.append(["local", "producto", "grupo", "variante", "nota"])
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    return path


GROUP_ROWS = [
    ("AMBOS", "Tonkotsu", "Tonkotsu", "Tonkotsu", ""),
    ("AMBOS", "Tantanmen carnívoro", "Tantanmen", "Carnívoro", ""),
    ("AMBOS", "Tantanmen vegano", "Tantanmen", "Vegano / vegetariano", "vegano y vegetariano juntos"),
    ("AMBOS", "Tantanmen vegetariano", "Tantanmen", "Vegano / vegetariano", "vegano y vegetariano juntos"),
    ("AMBOS", "Agua", "NO INCLUIR", "", "bebida"),
]


def site_frames(products: dict[str, float], weekdays: set[int], *, start: date, days: int, sigma: float = 2.0,
                sold: float | None = None, history_end: date | None = None):
    """Forecast of `products` (units per open day) and the matching sales history."""
    dates = [start + timedelta(days=i) for i in range(days) if (start + timedelta(days=i)).isoweekday() in weekdays]
    forecast = pl.DataFrame([{"fecha": d, "producto": p, "demanda_predicha": mu, "sigma_utilizada": sigma}
                             for p, mu in products.items() for d in dates])
    end = history_end or start - timedelta(days=1)
    past = [end - timedelta(days=i) for i in range(14) if (end - timedelta(days=i)).isoweekday() in weekdays]
    history = pl.DataFrame([{"fecha": d, "producto": p, "observado": mu if sold is None else sold}
                            for p, mu in products.items() for d in past])
    return forecast, history


def prepared(*args, **kw):
    return prepare_outputs(*site_frames(*args, **kw))


class WindowTests(unittest.TestCase):
    def test_default_week_is_next_friday_to_thursday(self):
        self.assertEqual(list(report_window(TODAY)), WEEK)
        self.assertEqual(report_window(date(2026, 11, 20))[0], date(2026, 11, 27))   # Friday -> next Friday
        self.assertEqual(report_window(date(2026, 11, 19))[0], date(2026, 11, 20))   # Thursday -> tomorrow

    def test_start_today_option(self):
        days = report_window(TODAY, start_today=True)
        self.assertEqual((days[0], days[-1]), (TODAY, TODAY + timedelta(days=6)))
        blocks = cycle_blocks(days)
        self.assertEqual([b.name for b in blocks], ["Mar–Jue", "Vie–Lun", "Mar–Jue"])
        self.assertEqual([len(b.days) for b in blocks], [2, 4, 1])

    def test_default_week_has_two_cycles(self):
        blocks = cycle_blocks(report_window(TODAY))
        self.assertEqual([(b.name, b.dates) for b in blocks], [("Vie–Lun", "20/11–23/11"), ("Mar–Jue", "24/11–26/11")])


class CalculationTests(unittest.TestCase):
    def test_production_covers_95_percent_and_rounds_up(self):
        # Real MUT case: Tantanmen carnívoro Fri-Mon, four days with sigma 8.78.
        self.assertEqual(production_quantity(137.41, 4 * 8.78 ** 2, Z95), 167)
        self.assertEqual(production_quantity(10.0, 0.0, Z95), 10)
        self.assertEqual(production_quantity(0.0, 25.0, Z95), 0)

    def test_traffic_light_thresholds(self):
        cases = [(114, 100, "verde", "Estable"), (115, 100, "amarillo", "Alza"), (130, 100, "amarillo", "Alza"),
                 (131, 100, "rojo", "Alza"), (80, 100, "amarillo", "Baja"), (60, 100, "rojo", "Baja"),
                 (114.6, 100, "amarillo", "Alza"), (130.4, 100, "amarillo", "Alza"), (130.6, 100, "rojo", "Alza"),
                 (10, 0, "sin_base", "Sin ventas previas")]
        for forecast, actual, level, word in cases:
            with self.subTest(forecast=forecast):
                trend = Trend(forecast, actual)
                self.assertEqual((trend.level, trend.word), (level, word))
        self.assertEqual(Trend(118, 100).percent, "+18%")
        self.assertEqual(Trend(114.6, 100).percent, "+15%")          # printed value and colour agree
        self.assertEqual(Trend(69.6, 100).percent, "-30%")

    def test_names_ignore_case_accents_and_spaces(self):
        self.assertEqual(normalize_name("TONKOTSU KIDS "), normalize_name("Tonkotsu  kids"))
        self.assertEqual(normalize_name("TANTANMEN CARNIVORO"), normalize_name("Tantanmen carnívoro"))
        self.assertEqual(normalize_name("Maze_Udon"), normalize_name("Maze Udon"))


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.mapping = read_group_mapping(write_groups(Path(self.temporary.name) / "g.xlsx", GROUP_ROWS))
        start = date(2026, 11, 19)
        self.outputs = {
            "MUT": prepared({"Tonkotsu": 10, "Tantanmen carnívoro": 6, "Tantanmen vegano": 2,
                                "Tantanmen vegetariano": 1, "Agua": 30, "Plato nuevo": 3},
                               {1, 2, 3, 4, 5, 6, 7}, start=start, days=21),
            "FRANKLIN": prepared({"TONKOTSU": 40, "TANTANMEN CARNIVORO": 20}, {5, 6, 7}, start=start, days=21,
                                    sold=20),
        }

    def build(self, **kw):
        return build_report(self.outputs, self.mapping, service_level=0.95, today=TODAY, **kw)

    def test_aggregates_sites_variants_and_cycles(self):
        data = self.build()
        tonkotsu, tantanmen = data.groups
        self.assertEqual([g.name for g in data.groups], ["Tonkotsu", "Tantanmen"])
        # MUT Tonkotsu Fri-Mon: 4 days x 10, sigma 2 each day.
        mut_friday = production_quantity(40, 4 * 4, Z95)
        franklin_friday = production_quantity(120, 3 * 4, Z95)
        self.assertEqual(tonkotsu.sites["MUT"][0].produce, [mut_friday, production_quantity(30, 3 * 4, Z95)])
        self.assertEqual(tonkotsu.sites["FRANKLIN"][0].produce, [franklin_friday, 0])     # closed Mon-Thu
        self.assertEqual(tonkotsu.produce(0), mut_friday + franklin_friday)
        self.assertEqual(tonkotsu.total, sum(line.total for lines in tonkotsu.sites.values() for line in lines))
        self.assertGreater(tonkotsu.total, tonkotsu.forecast_total)
        # Vegano and vegetariano share one row and one 95% margin; the note is kept.
        variants = [line.variant for line in tantanmen.variants]
        self.assertEqual(variants, ["Carnívoro", "Vegano / vegetariano"])
        self.assertEqual(tantanmen.sites["MUT"][1].produce[0], production_quantity(12, 4 * 8, Z95))
        self.assertEqual(tantanmen.notes, ["vegano y vegetariano juntos"])
        self.assertEqual(tonkotsu.daily_forecast, [50, 50, 50, 10, 10, 10, 10])

    def test_trends_unmapped_and_excluded_products(self):
        data = self.build()
        tonkotsu = data.groups[0]
        self.assertEqual(tonkotsu.trends["MUT"].level, "verde")                     # same as last week
        self.assertEqual(tonkotsu.trends["FRANKLIN"].word, "Alza")                  # 120 vs 60 sold
        self.assertEqual(tonkotsu.trends["FRANKLIN"].percent, "+100%")
        self.assertEqual([(site, product) for site, product, _ in data.unmapped], [("MUT", "Plato nuevo")])

    def test_products_without_recent_sales_are_not_produced(self):
        # A renamed dish: the model still forecasts the old name, which has not sold for three weeks.
        forecast, history = self.outputs["MUT"]
        old = date(2026, 10, 28)
        self.outputs["MUT"] = (
            pl.concat([forecast, forecast.filter(pl.col("producto") == "Tonkotsu")
                       .with_columns(pl.lit("Tonkotsu antiguo").alias("producto"))]),
            pl.concat([history, pl.DataFrame({"fecha": [old], "producto": ["Tonkotsu antiguo"], "observado": [9.0]})]))
        rows = self.mapping.rules
        self.mapping = type(self.mapping)(self.mapping.groups, {**rows, ("MUT", "tonkotsu antiguo"): rows[("MUT", "tonkotsu")]})
        data = self.build()
        self.assertEqual([(s, p, last) for s, p, _u, last in data.inactive], [("MUT", "Tonkotsu antiguo", old)])
        # The old name adds nothing: MUT still produces exactly what the active "Tonkotsu" needs.
        self.assertEqual(data.groups[0].sites["MUT"][0].produce,
                         [production_quantity(40, 16, Z95), production_quantity(30, 12, Z95)])

    def test_forecast_must_cover_the_week(self):
        self.outputs["MUT"] = prepared({"Tonkotsu": 10}, {1, 2, 3, 4, 5, 6, 7}, start=date(2026, 10, 1), days=28)
        with self.assertRaisesRegex(ReportError, "MUT.*Carga ventas más recientes"):
            self.build()

    def test_start_today_before_a_closed_site_opens(self):
        # Wednesday 18/11, data up to Tuesday: MUT forecasts from Wednesday, Franklin only from Friday.
        start = date(2026, 11, 18)
        self.outputs["MUT"] = prepared({"Tonkotsu": 10}, {1, 2, 3, 4, 5, 6, 7}, start=start, days=21)
        self.outputs["FRANKLIN"] = prepared({"TONKOTSU": 40}, {5, 6, 7}, start=start, days=21)
        data = self.build(start_today=True)
        self.assertEqual(data.days[0], TODAY)
        self.assertEqual(data.groups[0].sites["FRANKLIN"][0].produce[0], 0)       # Wed-Thu: Franklin closed

    def test_start_today_with_todays_sales_already_loaded(self):
        start = date(2026, 11, 19)                       # sales recorded up to today, the 18th
        self.outputs["MUT"] = prepared({"Tonkotsu": 10}, {1, 2, 3, 4, 5, 6, 7}, start=start, days=21)
        with self.assertRaisesRegex(ReportError, "ya tiene ventas registradas.*Desactiva"):
            self.build(start_today=True)

    def test_stale_sales_are_flagged(self):
        franklin, _history = self.outputs["FRANKLIN"]
        self.outputs["FRANKLIN"] = (franklin, _history.filter(pl.col("fecha") < date(2026, 11, 9)))
        data = self.build()
        self.assertEqual(data.last_data["FRANKLIN"], date(2026, 11, 8))
        self.assertEqual(data.stale_sites, ["FRANKLIN"])

    @unittest.skipUnless(importlib.util.find_spec("reportlab"), "reportlab is not installed")
    def test_pdf_is_written(self):
        from src.report import render_pdf
        path = render_pdf(self.build(), Path(self.temporary.name) / "r.pdf")
        content = path.read_bytes()
        self.assertTrue(content.startswith(b"%PDF"))
        self.assertGreater(content.count(b"/Type /Page"), 3)          # summary, two groups, unmapped list


class SourceTests(unittest.TestCase):
    def test_unknown_spread_uses_poisson_not_zero(self):
        forecast = pl.DataFrame({"fecha": [date(2026, 11, 20)] * 3, "producto": ["A", "B", "C"],
                                 "demanda_predicha": [9.0, 4.0, 2.0], "sigma_utilizada": [None, float("nan"), 1.5],
                                 "sigma_global": [None, None, 3.0]})
        history = pl.DataFrame({"fecha": [date(2026, 11, 19)], "producto": ["A"], "observado": [9.0]})
        prepared_forecast, _history = prepare_outputs(forecast, history)
        self.assertEqual(prepared_forecast["varianza"].to_list(), [9.0, 4.0, 2.25])

    def test_reads_what_the_latest_run_wrote(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            old = pl.DataFrame({"fecha": [date(2026, 11, 20)], "producto": ["Tonkotsu"], "demanda_predicha": [1.0],
                                "sigma_utilizada": [1.0]})
            history = pl.DataFrame({"fecha": [date(2026, 11, 19)], "producto": ["Tonkotsu"], "observado": [1.0]})
            old.write_parquet(output / "pronosticos.parquet")              # left by an earlier run
            history.write_parquet(output / "historico.parquet")
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Pronosticos"
            sheet.append(["fecha", "producto", "demanda_predicha", "sigma_utilizada"])
            sheet.append([date(2026, 11, 20), "Tonkotsu", 50.0, 2.0])
            sheet = workbook.create_sheet("Historico")
            sheet.append(["fecha", "producto", "observado"])
            sheet.append([date(2026, 11, 19), "Tonkotsu", 45.0])
            workbook.save(output / "modelo.xlsx")
            (output / "config_used.json").write_text('{"export_parquet": false}', encoding="utf-8")
            self.assertEqual(read_site_outputs(output, "modelo.xlsx")[0]["mu"].to_list(), [50.0])
            (output / "config_used.json").write_text('{"export_parquet": true}', encoding="utf-8")
            self.assertEqual(read_site_outputs(output, "modelo.xlsx")[0]["mu"].to_list(), [1.0])
            (output / "config_used.json").unlink()                          # no snapshot: newest files win
            self.assertEqual(read_site_outputs(output, "modelo.xlsx")[0]["mu"].to_list(), [50.0])


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.groups = write_groups(self.root / "g.xlsx", GROUP_ROWS)
        self.logger = logging.getLogger("report-tests")
        self.sites = {}
        start = date(2026, 11, 19)
        for site, products, weekdays in (("MUT", {"Tonkotsu": 10}, {1, 2, 3, 4, 5, 6, 7}),
                                         ("FRANKLIN", {"TONKOTSU": 30}, {5, 6, 7})):
            output = self.root / "out" / site.lower()
            output.mkdir(parents=True)
            forecast, history = site_frames(products, weekdays, start=start, days=21)
            forecast.write_parquet(output / "pronosticos.parquet")
            history.write_parquet(output / "historico.parquet")
            self.sites[site] = (output, f"modelo_demanda_{site}.xlsx")

    def generate(self, sites):
        return generate_report(self.root, sites, self.groups, self.logger, service_level=0.95, today=TODAY)

    def test_needs_both_restaurants(self):
        with self.assertLogs(self.logger, "INFO"):
            self.assertIsNone(self.generate({"MUT": self.sites["MUT"]}))
        status = read_status(self.root / "out" / "reporte")
        self.assertEqual(status["estado"], "omitido")
        self.assertIn("Franklin", status["mensaje"])

    @unittest.skipUnless(importlib.util.find_spec("reportlab"), "reportlab is not installed")
    def test_writes_pdf_and_status(self):
        path = self.generate(self.sites)
        self.assertEqual(path.name, "reporte_produccion_2026-11-20.pdf")
        status = read_status(path.parent)
        self.assertEqual((status["estado"], status["archivo"]), ("generado", path.name))

    def test_problems_are_recorded_not_raised(self):
        self.groups.unlink()
        with self.assertLogs(self.logger, "WARNING"):
            self.assertIsNone(self.generate(self.sites))
        self.assertEqual(read_status(self.root / "out" / "reporte")["estado"], "error")

    def test_reads_the_excel_workbook_without_parquet(self):
        output, _name = self.sites["MUT"]
        forecast, history = pl.read_parquet(output / "pronosticos.parquet"), pl.read_parquet(output / "historico.parquet")
        workbook = Workbook()
        for title, frame in (("Pronosticos", forecast), ("Historico", history)):
            sheet = workbook.create_sheet(title)
            sheet.append(frame.columns)
            for row in frame.iter_rows():
                sheet.append(list(row))
        workbook.save(output / "modelo_demanda_MUT.xlsx")
        (output / "pronosticos.parquet").unlink()
        excel_forecast, excel_history = read_site_outputs(output, "modelo_demanda_MUT.xlsx")
        self.assertEqual(excel_forecast.height, forecast.height)
        self.assertAlmostEqual(excel_forecast["mu"].sum(), forecast["demanda_predicha"].sum())
        self.assertEqual(excel_history["fecha"].max(), history["fecha"].max())


class MappingTests(unittest.TestCase):
    def test_repository_mapping_is_valid(self):
        mapping = read_group_mapping(REPOSITORY_ROOT / REPORT_GROUPS_WORKBOOK)
        self.assertEqual(mapping.groups[:4], ("Tonkotsu", "Tantanmen", "Udon", "Entradas"))
        self.assertEqual(mapping.lookup("FRANKLIN", "TANTANMEN VEGETARIANO").variant, "Vegano / vegetariano")
        self.assertEqual(mapping.lookup("MUT", "TONKOTSU KIDS ").group, "Tonkotsu")
        self.assertEqual(mapping.lookup("MUT", "Kombucha Jengibre").group, "")         # NO INCLUIR

    def test_invalid_rows_are_explained(self):
        with tempfile.TemporaryDirectory() as directory:
            for rows, message in (([("LONDRES", "Tonkotsu", "Tonkotsu", "", "")], "local debe ser"),
                                  ([("MUT", "", "Tonkotsu", "", "")], "faltan producto"),
                                  ([("MUT", "Tonkotsu", "A", "", ""), ("MUT", "TONKOTSU", "B", "", "")],
                                   "ya está asignado en MUT"),
                                  ([("AMBOS", "Tonkotsu", "A", "", ""), ("AMBOS", "tonkotsu", "B", "", "")],
                                   "ya está asignado en AMBOS")):
                with self.subTest(message=message):
                    path = write_groups(Path(directory) / "g.xlsx", rows)
                    with self.assertRaisesRegex(ReportError, message):
                        read_group_mapping(path)


class MappingPrecedenceTests(unittest.TestCase):
    def test_site_row_overrides_ambos_in_any_order(self):
        with tempfile.TemporaryDirectory() as directory:
            for rows in ([("AMBOS", "Tonkotsu", "Tonkotsu", "Normal", ""), ("MUT", "TONKOTSU", "Tonkotsu", "MUT", "")],
                         [("MUT", "TONKOTSU", "Tonkotsu", "MUT", ""), ("AMBOS", "Tonkotsu", "Tonkotsu", "Normal", "")]):
                mapping = read_group_mapping(write_groups(Path(directory) / "g.xlsx", rows))
                self.assertEqual(mapping.lookup("MUT", "Tonkotsu").variant, "MUT")
                self.assertEqual(mapping.lookup("FRANKLIN", "Tonkotsu").variant, "Normal")


class PipelineTests(unittest.TestCase):
    def test_report_is_written_before_the_inventory_can_fail(self):
        from src.runtime import PipelineError
        calls = []

        def run(command, *, label, **kwargs):
            calls.append(label)
            if label == "Inventory model":
                raise PipelineError("Inventory model failed")

        with tempfile.TemporaryDirectory() as directory, \
             patch("main.find_rscript", return_value=Path("Rscript")), \
             patch("main.r_environment", return_value={"R_LIBS_USER": "lib"}), \
             patch("main.run_logged", side_effect=run), \
             patch("src.elt.ELT") as elt, \
             patch("src.report.generate_report", side_effect=lambda *a, **k: calls.append("report")):
            root = Path(directory)
            for relative in ("scripts/prepare_r.R", "scripts/modelo_demanda_Franklin.R", "scripts/modelo_demanda_MUT.R",
                             "scripts/modelo_inventario.R", "settings/inventario_parametros.xlsx"):
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text("#")
            elt.return_value.run.return_value.failed = 0
            with self.assertRaises(PipelineError):
                application.run_pipeline(root, logging.getLogger("order-test"))
        self.assertLess(calls.index("report"), calls.index("Inventory model"))

    def test_main_passes_start_today_to_the_report(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("main.run_pipeline") as pipeline:
            self.assertEqual(application.main(Path(directory), report_start_today=True), 0)
            self.assertTrue(pipeline.call_args.kwargs["report_start_today"])


if __name__ == "__main__":
    unittest.main()
