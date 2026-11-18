"""Inventory configuration, editable parameters workbook and native R integration."""

import io
import json
import math
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook, load_workbook

from settings.config import (INVENTORY_PARAMETERS_WORKBOOK, INVENTORY_SETTINGS, INVENTORY_SITE_SETTINGS,
                             MODEL_SETTINGS, get_inventory_config)
from src.interface import PARAMETERS_IN_RUN, create_run, default_parameters_workbook, validate_parameters_workbook
from src.runtime import find_rscript, r_environment


REPOSITORY = Path(__file__).resolve().parents[1]


def parameters_workbook(shelf=(("caldo tonkotsu", 3, "entrevista"),),
                        composition=(("AMBOS", "Tonkotsu", "caldo tonkotsu", 1, "menu"),)) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "vida_util"
    sheet.append(["ingrediente", "vida_util_dias", "fuente"])
    for row in shelf:
        sheet.append(list(row))
    sheet = workbook.create_sheet("composicion")
    sheet.append(["local", "producto", "ingrediente", "cantidad_por_plato", "fuente"])
    for row in composition:
        sheet.append(list(row))
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


class InventoryConfigTests(unittest.TestCase):
    def test_defaults_follow_the_interviewed_replenishment_calendar(self):
        config = get_inventory_config()
        sites = {site["site"]: site for site in config["sites"]}
        self.assertEqual(sites["MUT"]["delivery_weekdays"], (2, 5))
        self.assertEqual(sites["FRANKLIN"]["delivery_weekdays"], (5,))
        self.assertTrue(sites["FRANKLIN"]["transfer_leftovers"])
        self.assertEqual(sites["FRANKLIN"]["operating_weekdays"], (5, 6, 7))
        self.assertTrue(sites["MUT"]["demand_workbook"].endswith("out/mut/modelo_demanda_MUT.xlsx"))
        self.assertEqual(Path(config["output_directory"]), REPOSITORY / "out" / "inventario")
        self.assertEqual(Path(config["parameters_workbook"]), REPOSITORY / INVENTORY_PARAMETERS_WORKBOOK)
        self.assertEqual(set(config["required_r_packages"]), {"jsonlite", "readxl", "writexl", "arrow"})

    def test_ui_values_reach_the_contract_without_changing_defaults(self):
        demand = {"MUT": replace(MODEL_SETTINGS["MUT"], horizon_days=14)}
        sites = {"MUT": replace(INVENTORY_SITE_SETTINGS["MUT"], delivery_weekdays=(1, 4), shortage_cost=5.0)}
        config = get_inventory_config(demand, site_settings=sites,
                                      settings=replace(INVENTORY_SETTINGS, confidence_level=0.9, export_parquet=False))
        self.assertEqual(len(config["sites"]), 1)
        self.assertEqual(config["sites"][0]["delivery_weekdays"], (1, 4))
        self.assertEqual(config["sites"][0]["horizon_days"], 14)
        self.assertEqual(config["confidence_level"], 0.9)
        self.assertNotIn("arrow", config["required_r_packages"])
        self.assertEqual(INVENTORY_SITE_SETTINGS["MUT"].delivery_weekdays, (2, 5))

    def test_invalid_settings_are_rejected(self):
        site_changes = ({"delivery_weekdays": ()}, {"delivery_weekdays": (2, 2)}, {"delivery_weekdays": (0,)},
                        {"shortage_cost": 0},
                        {"shortage_cost": True}, {"transfer_leftovers": "yes"})
        for changes in site_changes:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                get_inventory_config(site_settings={"MUT": replace(INVENTORY_SITE_SETTINGS["MUT"], **changes)})
        for changes in ({"confidence_level": 1}, {"default_service_level": 0}, {"min_fractile": 0.9, "max_fractile": 0.6},
                        {"output_subdirectory": "../private"}, {"export_excel": False, "export_parquet": False},
                        {"enabled": "yes"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                get_inventory_config(settings=replace(INVENTORY_SETTINGS, **changes))
        with self.assertRaises(ValueError):
            get_inventory_config({"MUT": replace(MODEL_SETTINGS["MUT"], export_excel=False)})

    def test_cli_emits_inventory_json(self):
        completed = subprocess.run([sys.executable, str(REPOSITORY / "settings" / "config.py"), "--inventory"],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["model"], "inventory")


class ParametersWorkbookTests(unittest.TestCase):
    def test_repository_workbook_is_valid_and_complete(self):
        validate_parameters_workbook(default_parameters_workbook())
        workbook = load_workbook(REPOSITORY / INVENTORY_PARAMETERS_WORKBOOK, read_only=True)
        self.assertEqual(workbook.sheetnames[:2], ["vida_util", "composicion"])
        header = next(workbook["vida_util"].iter_rows(max_row=1, values_only=True))
        self.assertEqual(list(header), ["ingrediente", "vida_util_dias", "fuente"])
        products = {row[1] for row in workbook["composicion"].iter_rows(min_row=2, values_only=True)}
        self.assertTrue({"Tonkotsu", "Tantanmen Carnívoro", "Coconut Mochi", "HMK"} <= products)

    @staticmethod
    def without_shelf_sheet() -> bytes:
        workbook = load_workbook(io.BytesIO(parameters_workbook()))
        del workbook["vida_util"]
        stream = io.BytesIO()
        workbook.save(stream)
        return stream.getvalue()

    def test_invalid_workbooks_explain_the_problem(self):
        cases = {
            "Excel válido": b"not an excel",
            "falta la hoja": self.without_shelf_sheet(),
            "no está en la hoja vida_util": parameters_workbook(composition=(("MUT", "Ramen", "desconocido", 1, ""),)),
            "cantidad_por_plato": parameters_workbook(composition=(("MUT", "Tonkotsu", "caldo tonkotsu", -1, ""),)),
            "local": parameters_workbook(composition=(("TIENDA", "Tonkotsu", "caldo tonkotsu", 1, ""),)),
            "repetido": parameters_workbook(shelf=(("caldo", 3, ""), ("Caldo", 4, ""))),
            "positiva": parameters_workbook(shelf=(("caldo tonkotsu", 0, ""),)),
            "numérica positiva": parameters_workbook(shelf=(("caldo tonkotsu", "NaN", ""),)),
            "debe ser un número positivo": parameters_workbook(
                composition=(("MUT", "Tonkotsu", "caldo tonkotsu", "inf", ""),)),
        }
        for message, content in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_parameters_workbook(content)
        for value in ("inf", "-inf", "NaN"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "numérica positiva"):
                validate_parameters_workbook(parameters_workbook(shelf=(("caldo tonkotsu", value, ""),)))

    def test_decimal_comma_is_accepted(self):
        validate_parameters_workbook(parameters_workbook(shelf=(("caldo tonkotsu", "2,5", ""),),
                                                         composition=(("AMBOS", "Tonkotsu", "caldo tonkotsu", "0,5", ""),)))

    def test_run_stores_uploaded_or_default_parameters(self):
        upload = {"MUT": [("ventas.xlsx", parameters_workbook())]}
        with tempfile.TemporaryDirectory() as directory:
            custom = create_run(Path(directory), upload, {"MUT": MODEL_SETTINGS["MUT"]}, parameters_workbook())
            self.assertEqual((custom / PARAMETERS_IN_RUN).read_bytes(), parameters_workbook())
            default = create_run(Path(directory), upload, {"MUT": MODEL_SETTINGS["MUT"]})
            self.assertEqual((default / PARAMETERS_IN_RUN).read_bytes(), default_parameters_workbook())
            with self.assertRaises(ValueError):
                create_run(Path(directory), upload, {"MUT": MODEL_SETTINGS["MUT"]}, b"broken")


class InventoryModelIntegrationTests(unittest.TestCase):
    """Run scripts/modelo_inventario.R on a synthetic demand workbook."""

    def setUp(self):
        self.rscript = find_rscript(REPOSITORY)
        if self.rscript is None:
            raise unittest.SkipTest("Rscript is not installed")
        check = subprocess.run([str(self.rscript), "--vanilla", "-e",
                                "quit(status=if(all(vapply(c('jsonlite','readxl','writexl'), requireNamespace, "
                                "logical(1), quietly=TRUE))) 0 else 1)"], capture_output=True,
                               env=r_environment(REPOSITORY, self.rscript))  # same library path as the real run
        if check.returncode:
            raise unittest.SkipTest("R packages jsonlite, readxl or writexl are missing")

    def run_model(self, root: Path, rows, parameters: bytes, history=None) -> tuple[subprocess.CompletedProcess, callable]:
        """Write a MUT demand workbook with `rows` (fecha, producto, mu, sigma), run the inventory model.

        history: optional Historico rows (fecha, producto, observado) for the inactive-product rule.
        """
        demand = root / "out" / "mut"
        demand.mkdir(parents=True)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Pronosticos"
        sheet.append(["fecha", "producto", "demanda_predicha", "sigma_utilizada"])
        for row in rows:
            sheet.append(list(row))
        if history is not None:
            sheet = workbook.create_sheet("Historico")
            sheet.append(["fecha", "producto", "observado"])
            for row in history:
                sheet.append(list(row))
        workbook.save(demand / "modelo_demanda_MUT.xlsx")
        (root / "parametros.xlsx").write_bytes(parameters)
        config = get_inventory_config({"MUT": MODEL_SETTINGS["MUT"]}, repository_root=root,
                                      parameters_workbook=root / "parametros.xlsx",
                                      settings=replace(INVENTORY_SETTINGS, export_parquet=False))
        (root / "inventory.json").write_text(json.dumps(config), encoding="utf-8")
        environment = {**r_environment(REPOSITORY, self.rscript), "MIRAI_INVENTORY_CONFIG": str(root / "inventory.json")}
        completed = subprocess.run([str(self.rscript), "--vanilla", str(REPOSITORY / "scripts" / "modelo_inventario.R")],
                                   env=environment, capture_output=True, text=True, encoding="utf-8")

        def rows_of(name):
            result = load_workbook(root / "out" / "inventario" / "modelo_inventario.xlsx", read_only=True)
            values = list(result[name].iter_rows(values_only=True))
            return [dict(zip(values[0], row)) for row in values[1:]]
        return completed, rows_of

    def week(self, product="TONKOTSU", mu=10.0, sigma=2.0):
        start = date(2026, 11, 16)  # Monday
        return [(start + timedelta(days=offset), product, mu, sigma) for offset in range(7)]

    def test_site_row_overrides_ambos_in_any_order(self):
        for composition in ((("AMBOS", "Tonkotsu", "caldo tonkotsu", 1, ""), ("MUT", "Tonkotsu", "caldo tonkotsu", 2, "")),
                            (("MUT", "Tonkotsu", "caldo tonkotsu", 2, ""), ("AMBOS", "Tonkotsu", "caldo tonkotsu", 1, ""))):
            with self.subTest(first=composition[0][0]), tempfile.TemporaryDirectory() as directory:
                completed, rows = self.run_model(Path(directory), self.week(), parameters_workbook(composition=composition))
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertAlmostEqual(rows("Resumen_semanal")[0]["porciones_esperadas"], 140.0)   # 70 dishes x 2

    def test_repeated_composition_rows_are_rejected_in_r(self):
        # main.bat runs R without the interface validator, so R must reject it too.
        composition = (("MUT", "Tonkotsu", "caldo tonkotsu", 1, ""), ("MUT", "TONKOTSU", "Caldo tonkotsu", 2, ""))
        with tempfile.TemporaryDirectory() as directory:
            completed, _rows = self.run_model(Path(directory), self.week(), parameters_workbook(composition=composition))
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("more than once", completed.stdout + completed.stderr)

    def test_products_without_recent_sales_are_left_out(self):
        rows = self.week() + self.week("Tonkotsu antiguo")
        last = date(2026, 11, 15)
        history = [(last - timedelta(days=d), "TONKOTSU", 10.0) for d in range(7)]
        history += [(date(2026, 10, 1), "Tonkotsu antiguo", 8.0)]               # last sold six weeks earlier
        rows += self.week("tonkotsu")                                           # old spelling of an active dish
        history += [(date(2026, 8, 1), "tonkotsu", 5.0)]
        composition = (("AMBOS", "Tonkotsu", "caldo tonkotsu", 1, ""), ("AMBOS", "Tonkotsu antiguo", "caldo tonkotsu", 1, ""))
        with tempfile.TemporaryDirectory() as directory:
            completed, result = self.run_model(Path(directory), rows, parameters_workbook(composition=composition), history)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertAlmostEqual(result("Resumen_semanal")[0]["porciones_esperadas"], 70.0)  # old name adds nothing
            self.assertEqual(sorted(r["producto"] for r in result("Productos_sin_ventas_recientes")),
                             ["Tonkotsu antiguo", "tonkotsu"])
            detail = result("Detalle_semanal_plato")[0]
            self.assertAlmostEqual(detail["platos_esperados"], 70.0)
            self.assertAlmostEqual(detail["porciones_esperadas"], 70.0)

    def test_no_product_with_composition_still_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            completed, rows = self.run_model(Path(directory), self.week("Agua"), parameters_workbook())
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(rows("Resumen_semanal"), [])
            self.assertEqual([row["producto"] for row in rows("Productos_sin_composicion")], ["Agua"])

    def test_decimal_comma_in_parameters(self):
        parameters = parameters_workbook(shelf=(("caldo tonkotsu", "3,0", ""),),
                                         composition=(("AMBOS", "Tonkotsu", "caldo tonkotsu", "0,5", ""),))
        with tempfile.TemporaryDirectory() as directory:
            completed, rows = self.run_model(Path(directory), self.week(), parameters)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertAlmostEqual(rows("Resumen_semanal")[0]["porciones_esperadas"], 35.0)   # 7 days x 10 x 0.5
            self.assertEqual(rows("Politica_ingrediente")[0]["vida_util_dias"], 3)

    def test_non_finite_parameters_are_rejected(self):
        for shelf, quantity in (("NaN", 1), ("inf", 1), (3, "inf")):
            with self.subTest(shelf=shelf, quantity=quantity), tempfile.TemporaryDirectory() as directory:
                parameters = parameters_workbook(shelf=(("caldo tonkotsu", shelf, ""),),
                                                 composition=(("AMBOS", "Tonkotsu", "caldo tonkotsu", quantity, ""),))
                completed, _rows = self.run_model(Path(directory), self.week(), parameters)
                self.assertNotEqual(completed.returncode, 0)
                self.assertRegex(completed.stdout + completed.stderr, "vida_util_dias|cantidad_por_plato")

    def test_unknown_spread_is_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            completed, rows = self.run_model(Path(directory), self.week(sigma=None), parameters_workbook())
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            weekly = rows("Resumen_semanal")[0]
            self.assertAlmostEqual(weekly["desviacion_estandar"], round(math.sqrt(70), 2), places=1)  # Poisson

    def test_weekly_interval_cycles_and_unmatched_products(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            demand = root / "out" / "mut"
            demand.mkdir(parents=True)
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Pronosticos"
            sheet.append(["fecha", "producto", "demanda_predicha", "sigma_utilizada"])
            start = date(2026, 11, 16)  # Monday
            for offset in range(14):
                sheet.append([start + timedelta(days=offset), "TONKOTSU", 10.0, 2.0])
                sheet.append([start + timedelta(days=offset), "Agua", 3.0, 1.0])
            workbook.save(demand / "modelo_demanda_MUT.xlsx")
            parameters = root / "parametros.xlsx"
            parameters.write_bytes(parameters_workbook())
            config = get_inventory_config({"MUT": MODEL_SETTINGS["MUT"]}, repository_root=root,
                                          parameters_workbook=parameters,
                                          settings=replace(INVENTORY_SETTINGS, export_parquet=False))
            config_path = root / "inventory.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            environment = {**r_environment(REPOSITORY, self.rscript), "MIRAI_INVENTORY_CONFIG": str(config_path)}
            completed = subprocess.run([str(self.rscript), "--vanilla", str(REPOSITORY / "scripts" / "modelo_inventario.R")],
                                       env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = load_workbook(root / "out" / "inventario" / "modelo_inventario.xlsx", read_only=True)

            def rows(name):
                values = list(result[name].iter_rows(values_only=True))
                return [dict(zip(values[0], row)) for row in values[1:]]

            weekly = rows("Resumen_semanal")
            self.assertEqual(len(weekly), 2)
            first = weekly[0]
            self.assertEqual(first["semana_inicio"], "2026-11-16")
            self.assertAlmostEqual(first["porciones_esperadas"], 70.0)
            half_width = 1.959964 * math.sqrt(7 * 4)
            self.assertAlmostEqual(first["ic_inferior"], round(70 - half_width, 1), places=1)
            self.assertAlmostEqual(first["ic_superior"], round(70 + half_width, 1), places=1)
            policy = rows("Politica_ingrediente")
            self.assertEqual([row["dia_entrega"] for row in policy[:2]], ["Martes", "Viernes"])
            self.assertNotIn("fecha_termino_preparacion", policy[0])
            # Preparations finish on the dispatch day: 3 days of shelf life cover Tue-Thu
            # but not the four days Fri-Mon, which need an intermediate replenishment.
            self.assertEqual(policy[0]["fecha_entrega"], "2026-11-17")
            self.assertEqual([row["restriccion_vida_util"] for row in policy[:2]], [False, True])
            self.assertAlmostEqual(policy[1]["demanda_fuera_vida_util"], 10.0)
            self.assertEqual([row["producto"] for row in rows("Productos_sin_composicion")], ["Agua"])
            uncovered = rows("Dias_sin_cobertura")
            self.assertEqual([row["fecha"] for row in uncovered], ["2026-11-16"])


if __name__ == "__main__":
    unittest.main()
