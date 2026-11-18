"""Editable defaults and replacement of the base parameters workbook."""

import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from openpyxl import load_workbook

from settings.config import (INVENTORY_PARAMETERS_WORKBOOK, Defaults, code_defaults, defaults_from_dict,
                             defaults_to_dict, load_user_defaults, reset_user_defaults, save_user_defaults,
                             user_defaults_path)
from src.interface import (compare_parameters_workbooks, default_parameters_workbook, read_parameters_workbook,
                           replace_default_parameters)


REPOSITORY = Path(__file__).resolve().parents[1]


def edited_base(remove_composition: int = 0, add_composition: tuple = (), edit_shelf: dict | None = None) -> bytes:
    """Repository base workbook with some composition rows removed/added and shelf-life values changed."""
    workbook = load_workbook(io.BytesIO(default_parameters_workbook()))
    composition = workbook["composicion"]
    for _ in range(remove_composition):
        composition.delete_rows(composition.max_row)
    for row in add_composition:
        composition.append(list(row))
    for row in workbook["vida_util"].iter_rows(min_row=2):
        if edit_shelf and row[0].value in edit_shelf:
            row[1].value = edit_shelf[row[0].value]
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


class UserDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "settings").mkdir()

    def test_missing_file_means_code_defaults(self):
        self.assertEqual(load_user_defaults(self.root), code_defaults())

    def test_saved_defaults_round_trip_and_reset(self):
        base = code_defaults()
        changed = Defaults(
            {site: replace(settings, horizon_days=14, generate_plots=False) for site, settings in base.model_settings.items()},
            {**base.inventory_sites, "MUT": replace(base.inventory_sites["MUT"], delivery_weekdays=(1, 4), shortage_cost=3.5)},
            replace(base.inventory, confidence_level=0.9))
        path = save_user_defaults(changed, self.root)
        self.assertEqual(path, user_defaults_path(self.root))
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["sites"]["MUT"]["delivery_weekdays"], [1, 4])
        self.assertEqual(stored["general"]["horizon_days"], 14)
        self.assertEqual(load_user_defaults(self.root), changed)
        reset_user_defaults(self.root)
        self.assertEqual(load_user_defaults(self.root), code_defaults())

    def test_partial_files_keep_code_values_for_missing_keys(self):
        loaded = defaults_from_dict({"schema_version": 1, "sites": {"MUT": {"delivery_weekdays": [3]}}})
        self.assertEqual(loaded.inventory_sites["MUT"].delivery_weekdays, (3,))
        self.assertEqual(loaded.inventory_sites["FRANKLIN"], code_defaults().inventory_sites["FRANKLIN"])
        self.assertEqual(loaded.model_settings["MUT"].horizon_days, 28)

    def test_obsolete_lead_days_key_is_ignored(self):
        loaded = defaults_from_dict({"schema_version": 1, "sites": {"MUT": {"preparation_lead_days": 1,
                                                                             "delivery_weekdays": [2]}}})
        self.assertEqual(loaded.inventory_sites["MUT"].delivery_weekdays, (2,))
        self.assertNotIn("preparation_lead_days", defaults_to_dict(loaded)["sites"]["MUT"])

    def test_invalid_files_are_rejected(self):
        valid = defaults_to_dict(code_defaults())
        cases = [
            {**valid, "schema_version": 2},
            {**valid, "extra": 1},
            {**valid, "general": {**valid["general"], "horizon_days": 0}},
            {**valid, "general": {**valid["general"], "unknown": 1}},
            {**valid, "sites": {**valid["sites"], "TIENDA": {}}},
            {**valid, "sites": {**valid["sites"], "MUT": {**valid["sites"]["MUT"], "delivery_weekdays": []}}},
            {**valid, "sites": {**valid["sites"], "MUT": {**valid["sites"]["MUT"], "model": "otro"}}},
            {**valid, "inventory": {"confidence_level": 1.5}},
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                defaults_from_dict(data)
        user_defaults_path(self.root).write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "valores_por_defecto.json"):
            load_user_defaults(self.root)


class BaseWorkbookTests(unittest.TestCase):
    def test_counts_added_and_removed_rows_per_sheet(self):
        new = edited_base(remove_composition=1,
                          add_composition=(("MUT", "Ramen nuevo", "caldo tonkotsu", 1, "x"),
                                           ("MUT", "Ramen nuevo", "huevo curado", 1, "x")),
                          edit_shelf={"caldo tonkotsu": 7})
        summary = {row["hoja"]: row for row in compare_parameters_workbooks(default_parameters_workbook(), new)}
        self.assertEqual((summary["composicion"]["añadidas"], summary["composicion"]["eliminadas"]), (2, 1))
        self.assertEqual(summary["composicion"]["filas_nuevas"], summary["composicion"]["filas_actuales"] + 1)
        self.assertEqual((summary["vida_util"]["añadidas"], summary["vida_util"]["eliminadas"]), (1, 1))
        unchanged = compare_parameters_workbooks(default_parameters_workbook(), default_parameters_workbook())
        self.assertTrue(all(row["añadidas"] == row["eliminadas"] == 0 for row in unchanged))

    def test_numbers_and_spacing_do_not_count_as_changes(self):
        workbook = load_workbook(io.BytesIO(default_parameters_workbook()))
        cell = workbook["vida_util"]["B2"]
        cell.value = f" {cell.value} "
        workbook["composicion"]["D2"].value = "1,0"
        stream = io.BytesIO()
        workbook.save(stream)
        summary = compare_parameters_workbooks(default_parameters_workbook(), stream.getvalue())
        self.assertTrue(all(row["añadidas"] == row["eliminadas"] == 0 for row in summary))

    def test_duplicate_dish_ingredient_is_rejected(self):
        first = read_parameters_workbook(default_parameters_workbook())["composicion"][0]
        with self.assertRaisesRegex(ValueError, "ya tiene"):
            read_parameters_workbook(edited_base(add_composition=(first,)))

    def test_replacement_keeps_a_backup_and_rejects_invalid_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / INVENTORY_PARAMETERS_WORKBOOK
            target.parent.mkdir(parents=True)
            target.write_bytes(default_parameters_workbook())
            with self.assertRaises(ValueError):
                replace_default_parameters(b"broken", root)
            self.assertEqual(target.read_bytes(), default_parameters_workbook())
            new = edited_base(remove_composition=2)
            backup = replace_default_parameters(new, root)
            self.assertEqual(target.read_bytes(), new)
            self.assertEqual(backup.read_bytes(), default_parameters_workbook())
            self.assertTrue(backup.is_relative_to(root / "out"))


if __name__ == "__main__":
    unittest.main()
