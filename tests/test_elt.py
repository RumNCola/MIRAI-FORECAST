"""Behavioral tests using synthetic exports; no private RAW data is required."""

import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

import pandas as pd

from src.old_elt import ELT, IntegrityError, _sha256


def write_excel_fixture(path, rows, sheet="Sheet1"):
    """Write a minimal inline-string OOXML fixture, including explicit error cells."""
    path.parent.mkdir(parents=True, exist_ok=True)
    xml_rows = []
    for number, values in enumerate(rows, 1):
        cells = []
        for index, value in enumerate(values):
            reference = f"{chr(65 + index)}{number}"
            if value is None:
                continue
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{reference}"><v>{value}</v></c>')
            elif value == "#REF!":
                cells.append(f'<c r="{reference}" t="e"><v>#REF!</v></c>')
            else:
                cells.append(f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">'
                             f'{escape(str(value))}</t></is></c>')
        xml_rows.append(f'<row r="{number}">{"".join(cells)}</row>')
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>''')
        archive.writestr("_rels/.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>''')
        archive.writestr("xl/workbook.xml", f'''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>
<sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>''')
        archive.writestr("xl/worksheets/sheet1.xml",
                         '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                         f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>')


class ELTTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.elt = ELT(self.root)
        self.elt.raw_dir.mkdir(parents=True)

    def log(self):
        return json.loads(self.elt.log_path.read_text(encoding="utf-8"))

    def mut_frame(self, **changes):
        values = {"date": "2026-10-01", "category": "Food", "product_name": "Gyosas vietnam",
                  "quantity": 2, "order_reference": "001-001", "order_id": "00001",
                  "product_sku": "0013", "status": "pagado", "time": "20:55:45"}
        values.update(changes)
        return pd.DataFrame([values])

    def save_mut(self, frame=None, name="orders.parquet", source="MUT_SALES"):
        path = self.elt.raw_dir / source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        (self.mut_frame() if frame is None else frame).to_parquet(path, index=False)
        return path

    def store_frame(self, quantity=-2, movement="Nota de crédito"):
        return pd.DataFrame([{
            "date": "2026-10-01", "category": "Products", "product_name": "CHILI OIL",
            "quantity": quantity, "movement": movement, "document": "NOTA DE CRÉDITO ELECTRÓNICA",
            "document_number": "00001", "variant": None,
        }])

    def test_import_does_not_process_or_write(self):
        self.save_mut()
        with patch.object(ELT, "run", side_effect=AssertionError("Unexpected processing")):
            importlib.import_module("src.old_elt")
        self.assertFalse(self.elt.log_path.exists())
        self.assertFalse(self.elt.del_dir.exists())

    def test_first_run_preserves_names_identifiers_and_repeated_lines(self):
        frame = pd.concat([self.mut_frame(product_name="KARAAGE KIDS ")] * 2, ignore_index=True)
        self.save_mut(frame)
        result = self.elt.run()
        self.assertEqual((result.processed, result.failed), (1, 0))
        output = pd.read_parquet(self.root / result.files[0].output)
        self.assertEqual(output["product_name"].tolist(), ["KARAAGE KIDS "] * 2)
        self.assertEqual(output["product_sku"].tolist(), ["0013"] * 2)
        self.assertEqual(output["order_id"].tolist(), ["00001"] * 2)
        self.assertEqual(output["quantity"].sum(), 4)
        self.assertEqual(output["source_row"].tolist(), [1, 2])
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(output["date"]))
        self.assertTrue(pd.api.types.is_integer_dtype(output["quantity"]))
        codes = {w["code"] for w in result.files[0].warnings}
        self.assertIn("repeated_lines_preserved", codes)
        self.assertIn("surrounding_whitespace_preserved", codes)

    def test_second_run_skips_without_rewriting_output(self):
        self.save_mut()
        first = self.elt.run()
        output = self.root / first.files[0].output
        before = output.stat().st_mtime_ns
        second = self.elt.run()
        self.assertEqual((second.processed, second.skipped, second.failed), (0, 1, 0))
        self.assertEqual(before, output.stat().st_mtime_ns)

    def test_changed_raw_is_reprocessed_and_new_name_is_reported(self):
        self.save_mut()
        first = self.elt.run()
        self.save_mut(self.mut_frame(product_name="Gyosas Vietnam", quantity=3))
        result = self.elt.run()
        self.assertEqual(result.processed, 1)
        self.assertEqual(first.files[0].output, result.files[0].output)
        warnings = {w["code"]: w for w in result.files[0].warnings}
        self.assertEqual(warnings["unrecognized_product_names"]["names"], ["Gyosas Vietnam"])
        self.assertEqual(self.log()["product_catalog"]["MUT_SALES"], ["Gyosas vietnam"])
        output = pd.read_parquet(self.root / result.files[0].output)
        self.assertEqual(output.iloc[0]["product_name"], "Gyosas Vietnam")
        self.assertEqual(output.iloc[0]["quantity"], 3)

    def test_missing_and_corrupted_outputs_are_rebuilt(self):
        self.save_mut()
        first = self.elt.run()
        output = self.root / first.files[0].output
        output.unlink()
        self.assertEqual(self.elt.run().processed, 1)
        output.write_bytes(b"broken parquet")
        self.assertEqual(self.elt.run().processed, 1)
        self.assertEqual(len(pd.read_parquet(output)), 1)

    def test_identical_copies_reuse_one_output(self):
        original = self.save_mut()
        shutil.copyfile(original, original.with_name("copy.parquet"))
        result = self.elt.run()
        self.assertEqual((result.processed, result.skipped), (1, 1))
        self.assertEqual(len(list(self.elt.del_dir.rglob("*.parquet"))), 1)

    def test_store_credit_note_signs(self):
        clean = self.elt.clean_franklin_store(self.store_frame())
        self.assertEqual(clean["quantity"].sum(), -2)
        with self.assertRaisesRegex(IntegrityError, "signs disagree"):
            self.elt.clean_franklin_store(self.store_frame(quantity=2))

    def test_invalid_records_are_rejected(self):
        for changes in ({"date": "not a date"}, {"date": 46300}, {"product_name": " "},
                        {"quantity": float("inf")}, {"quantity": 1.5}, {"quantity": 0},
                        {"quantity": -1}, {"quantity": True}, {"time": "25:00:00"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError):
                self.elt.clean_mut_sales(self.mut_frame(**changes))

    def test_failed_file_does_not_block_others_and_retains_previous_output(self):
        good = self.save_mut()
        accepted = self.elt.run()
        output = self.root / accepted.files[0].output
        digest = _sha256(output)
        self.save_mut(self.mut_frame(quantity=-1))
        self.save_mut(self.mut_frame(product_name="New item"), name="valid.parquet")
        result = self.elt.run()
        self.assertEqual((result.processed, result.failed), (1, 1))
        self.assertEqual(_sha256(output), digest)
        record = self.log()["files"][good.relative_to(self.root).as_posix()]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["last_success"]["output_sha256"], digest)
        self.assertFalse(self.elt.lock_path.exists())

    def test_corrupt_log_and_existing_lock_fail_without_resetting_state(self):
        self.elt.log_path.write_text("invalid json", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            self.elt.run()
        self.assertEqual(self.elt.log_path.read_text(), "invalid json")
        self.assertFalse(self.elt.lock_path.exists())
        self.elt.lock_path.write_text("other process", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.elt.run()
        self.assertEqual(self.elt.lock_path.read_text(), "other process")

    def test_excel_shifted_header_credit_note_and_physical_row(self):
        path = self.elt.raw_dir / "FRANKLIN_STORE" / "store.xlsx"
        rows = [[None], ["Store sales"], [None], ["Credit notes are negative"], [None],
                ["Fecha", "Movimiento", "Documento", "Folio", "Categoría", "Producto",
                 "Variante / presentación", "Cantidad"],
                ["2026-10-01", "Nota de crédito", "NOTA DE CRÉDITO ELECTRÓNICA", "0001",
                 "Products", "CHILI OIL", "210 CC", -2]]
        write_excel_fixture(path, rows, sheet="Detalle de ventas")
        result = self.elt.run()
        self.assertEqual((result.processed, result.failed), (1, 0))
        output = pd.read_parquet(self.root / result.files[0].output)
        self.assertEqual(output["source_row"].tolist(), [7])
        self.assertEqual(output["quantity"].tolist(), [-2])
        self.assertEqual(output["document_number"].tolist(), ["0001"])

    def test_mut_excel_preserves_populated_unlabeled_column_and_skips_lock(self):
        path = self.elt.raw_dir / "MUT_SALES" / "mut.xlsx"
        rows = [["Fecha", "Categoría", "Producto", "Cantidad", "Correlativo", "#Orden",
                 "SKU producto", "Estado", None],
                ["2026-10-01", "Food", "Gyosas vietnam", 1, "001-001", "0001", "0013", "pagado", "device-001"]]
        write_excel_fixture(path, rows)
        path.with_name("~$mut.xlsx").write_bytes(b"not an Excel file")
        result = self.elt.run()
        self.assertEqual((result.processed, result.failed), (1, 0))
        output = pd.read_parquet(self.root / result.files[0].output)
        self.assertEqual(output["source_unlabeled_9"].tolist(), ["device-001"])

    def test_excel_errors_and_duplicate_headers_fail(self):
        path = self.elt.raw_dir / "MUT_SALES" / "invalid.xlsx"
        headers = ["Fecha", "Categoría", "Producto", "Cantidad", "Correlativo", "#Orden", "SKU producto", "Estado"]
        for rows in ([headers, ["2026-10-01", "Food", "#REF!", 1, "001", "001", "013", "pagado"]],
                     [headers + ["Producto"], ["2026-10-01", "Food", "Item", 1, "001", "001", "013", "pagado", "Item"]]):
            with self.subTest(rows=rows):
                write_excel_fixture(path, rows)
                self.assertEqual(self.elt.run().failed, 1)
                self.assertFalse(self.elt.del_dir.exists())

    def test_ranking_mismatch_and_daily_duplicate_keys_fail(self):
        frame = pd.DataFrame([{"date": datetime(2026, 10, 1), "category": "Food", "subcategory": "Lunch",
                               "product_name": "Item", "quantity": 2}])
        clean = self.elt.clean_franklin_sales(frame)
        ranking = frame.drop(columns="date").assign(quantity=3)
        with self.assertRaisesRegex(IntegrityError, "reconcile"):
            self.elt._audit(frame, clean, "FRANKLIN_SALES", {"ranking": ranking}, None)
        with self.assertRaisesRegex(IntegrityError, "Duplicate daily"):
            self.elt.clean_franklin_sales(pd.concat([frame, frame], ignore_index=True))

    def test_catalog_bootstraps_entire_batch_and_overlap_is_reported(self):
        self.save_mut()
        self.save_mut(self.mut_frame(product_name="Second item"), name="second.parquet")
        result = self.elt.run()
        self.assertEqual(self.log()["product_catalog"]["MUT_SALES"], ["Gyosas vietnam", "Second item"])
        codes = {w["code"] for item in result.files for w in item.warnings}
        self.assertIn("overlapping_export_period", codes)
        self.assertNotIn("unrecognized_product_names", codes)

    def test_cli_works_outside_repository(self):
        script = Path(__file__).resolve().parents[1] / "src" / "old_elt.py"
        copied = self.root / "src" / "old_elt.py"
        copied.parent.mkdir()
        shutil.copyfile(script, copied)
        self.save_mut()
        completed = subprocess.run([sys.executable, str(copied)], cwd=self.root.parent,
                                   capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("1 processed", completed.stderr)


if __name__ == "__main__":
    unittest.main()
