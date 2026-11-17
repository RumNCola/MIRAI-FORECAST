"""Behavioral tests using synthetic exports; no private RAW data is required."""

import json
import io
import logging
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

import polars as pl

from src.old_elt import ELT, ELTError, IntegrityError, PIPELINE_VERSION, main, _sha256


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
        return pl.DataFrame([values])

    def save_mut(self, frame=None, name="orders.parquet", source="MUT_SALES"):
        path = self.elt.raw_dir / source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        (self.mut_frame() if frame is None else frame).write_parquet(path)
        return path

    def store_frame(self, quantity=-2, movement="Nota de crédito"):
        return pl.DataFrame([{
            "date": "2026-10-01", "category": "Products", "product_name": "CHILI OIL",
            "quantity": quantity, "movement": movement, "document": "NOTA DE CRÉDITO ELECTRÓNICA",
            "document_number": "00001", "variant": None,
        }])

    def test_import_does_not_process_or_write(self):
        self.save_mut()
        copied = self.root / "src" / "old_elt.py"
        copied.parent.mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[1] / "src" / "old_elt.py", copied)
        completed = subprocess.run(
            [sys.executable, "-c", "import src.old_elt"], cwd=self.root,
            capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(self.elt.log_path.exists())
        self.assertFalse(self.elt.del_dir.exists())

    def test_first_run_preserves_names_identifiers_and_repeated_lines(self):
        frame = pl.concat([self.mut_frame(product_name="KARAAGE KIDS ")] * 2)
        self.save_mut(frame)
        result = self.elt.run()
        self.assertEqual((result.processed, result.failed), (1, 0))
        output = pl.read_parquet(self.root / result.files[0].output)
        self.assertEqual(output["product_name"].to_list(), ["KARAAGE KIDS "] * 2)
        self.assertEqual(output["product_sku"].to_list(), ["0013"] * 2)
        self.assertEqual(output["order_id"].to_list(), ["00001"] * 2)
        self.assertEqual(output["quantity"].sum(), 4)
        self.assertEqual(output["source_row"].to_list(), [1, 2])
        self.assertEqual(output.schema["date"], pl.Datetime("us"))
        self.assertEqual(output.schema["quantity"], pl.Int64)
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
        output = pl.read_parquet(self.root / result.files[0].output)
        self.assertEqual(output.row(0, named=True)["product_name"], "Gyosas Vietnam")
        self.assertEqual(output.row(0, named=True)["quantity"], 3)

    def test_missing_and_corrupted_outputs_are_rebuilt(self):
        self.save_mut()
        first = self.elt.run()
        output = self.root / first.files[0].output
        output.unlink()
        self.assertEqual(self.elt.run().processed, 1)
        output.write_bytes(b"broken parquet")
        self.assertEqual(self.elt.run().processed, 1)
        self.assertEqual(pl.read_parquet(output).height, 1)

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
        with self.assertRaises(ELTError):
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
        output = pl.read_parquet(self.root / result.files[0].output)
        self.assertEqual(output["source_row"].to_list(), [7])
        self.assertEqual(output["quantity"].to_list(), [-2])
        self.assertEqual(output["document_number"].to_list(), ["0001"])

    def test_mut_excel_preserves_populated_unlabeled_column_and_skips_lock(self):
        path = self.elt.raw_dir / "MUT_SALES" / "mut.xlsx"
        rows = [["Fecha", "Categoría", "Producto", "Cantidad", "Correlativo", "#Orden",
                 "SKU producto", "Estado", None],
                ["2026-10-01", "Food", "Gyosas vietnam", 1, "001-001", "0001", "0013", "pagado", "device-001"]]
        write_excel_fixture(path, rows)
        path.with_name("~$mut.xlsx").write_bytes(b"not an Excel file")
        result = self.elt.run()
        self.assertEqual((result.processed, result.failed), (1, 0))
        output = pl.read_parquet(self.root / result.files[0].output)
        self.assertEqual(output["source_unlabeled_9"].to_list(), ["device-001"])

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
        frame = pl.DataFrame([{"date": datetime(2026, 10, 1), "category": "Food", "subcategory": "Lunch",
                               "product_name": "Item", "quantity": 2}])
        clean = self.elt.clean_franklin_sales(frame)
        ranking = frame.drop("date").with_columns(pl.lit(3).alias("quantity"))
        with self.assertRaisesRegex(IntegrityError, "reconcile"):
            self.elt._audit(frame, clean, "FRANKLIN_SALES", {"ranking": ranking}, None)
        with self.assertRaisesRegex(IntegrityError, "Duplicate daily"):
            self.elt.clean_franklin_sales(pl.concat([frame, frame]))

    def test_catalog_bootstraps_entire_batch_and_overlap_is_reported(self):
        self.save_mut()
        self.save_mut(self.mut_frame(product_name="Second item"), name="second.parquet")
        result = self.elt.run()
        self.assertEqual(self.log()["product_catalog"]["MUT_SALES"], ["Gyosas vietnam", "Second item"])
        codes = {w["code"] for item in result.files for w in item.warnings}
        self.assertIn("overlapping_export_period", codes)
        self.assertNotIn("unrecognized_product_names", codes)

    def test_partial_batch_defers_catalog_until_source_succeeds(self):
        self.save_mut()
        self.save_mut(self.mut_frame(quantity=-1), name="second.parquet")
        self.assertEqual(self.elt.run().failed, 1)
        self.assertNotIn("MUT_SALES", self.log()["product_catalog"])
        self.save_mut(self.mut_frame(product_name="Second item"), name="second.parquet")
        self.assertEqual(self.elt.run().failed, 0)
        self.assertEqual(self.log()["product_catalog"]["MUT_SALES"], ["Gyosas vietnam", "Second item"])

    def test_failed_parquet_write_is_not_marked_processed(self):
        self.save_mut()
        with patch.object(ELT, "_write_parquet", side_effect=OSError("Disk full")):
            with self.assertRaises(ELTError):
                self.elt.run()
        record = next(iter(self.log()["files"].values()))
        self.assertEqual(record["status"], "failed")
        self.assertEqual(self.elt.run().processed, 1)

    def test_store_summary_counts_fail_and_auxiliary_formula_errors_warn(self):
        original = self.store_frame()
        cleaned = self.elt.clean_franklin_store(original)
        with self.assertRaisesRegex(IntegrityError, "summary does not reconcile"):
            self.elt._audit(original, cleaned, "FRANKLIN_STORE", {"summary": {"filas de detalle": 100}}, None)
        report = self.elt._audit(original, cleaned, "FRANKLIN_STORE",
                                 {"summary": {"filas de detalle": 1}, "summary_errors": 3}, None)
        self.assertIn("excel_errors_in_auxiliary_summary", {w["code"] for w in report["warnings"]})

    def test_mut_store_alias_and_optional_monetary_columns(self):
        self.save_mut(self.mut_frame(currency="CLP", unit_price=12500, total_amount=25000), source="MUT_STORE")
        result = self.elt.run()
        self.assertEqual((result.processed, result.failed), (1, 0))
        output = pl.read_parquet(self.root / result.files[0].output)
        self.assertEqual(output.row(0, named=True)["source"], "MUT_STORE")
        self.assertEqual(output.row(0, named=True)["unit_price"], 12500)

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

    def events(self):
        return (self.root / "elt.log").read_text(encoding="utf-8")

    def test_output_uses_only_parquet_extension(self):
        path = self.elt.raw_dir / "MUT_SALES" / "sales.xlsx"
        headers = ["Fecha", "Categoría", "Producto", "Cantidad", "Correlativo", "#Orden", "SKU producto", "Estado"]
        write_excel_fixture(path, [headers, ["2026-10-01", "Food", "Item", 1, "001", "001", "013", "pagado"]])
        result = self.elt.run()
        self.assertEqual(result.files[0].output, "Data/DEL/MUT_SALES/sales.parquet")
        self.assertFalse(list(self.elt.del_dir.rglob("*.xlsx.parquet")))

    def test_legacy_output_is_migrated_without_losing_catalog(self):
        self.save_mut()
        first = self.elt.run()
        output = self.root / first.files[0].output
        legacy = output.with_name("orders.parquet.parquet")
        output.rename(legacy)
        log = self.log()
        record = next(iter(log["files"].values()))
        record["output"] = legacy.relative_to(self.root).as_posix()
        record["pipeline_version"] = "1"
        self.elt.log_path.write_text(json.dumps(log), encoding="utf-8")
        result = self.elt.run()
        self.assertEqual((result.processed, result.failed), (1, 0))
        self.assertTrue(output.is_file())
        self.assertFalse(legacy.exists())
        self.assertEqual(self.log()["product_catalog"], log["product_catalog"])
        self.assertEqual(next(iter(self.log()["files"].values()))["pipeline_version"], PIPELINE_VERSION)
        self.assertIn("Legacy output retired", self.events())

    def test_modified_legacy_output_is_retained_with_warning(self):
        self.save_mut()
        first = self.elt.run()
        output = self.root / first.files[0].output
        legacy = output.with_name("orders.parquet.parquet")
        output.rename(legacy)
        log = self.log()
        record = next(iter(log["files"].values()))
        record.update(output=legacy.relative_to(self.root).as_posix(), pipeline_version="1")
        self.elt.log_path.write_text(json.dumps(log), encoding="utf-8")
        legacy.write_bytes(b"manually modified")
        self.assertEqual(self.elt.run().processed, 1)
        self.assertEqual(legacy.read_bytes(), b"manually modified")
        self.assertIn("WARNING", self.events())
        self.assertIn("Legacy output was modified", self.events())

    def test_colliding_stems_stop_before_publication(self):
        path = self.save_mut()
        path.with_suffix(".xlsx").write_bytes(b"same output stem")
        with self.assertRaisesRegex(ELTError, "collision"):
            self.elt.run()
        self.assertFalse(self.elt.del_dir.exists())
        events = self.events()
        self.assertIn("ERROR", events)
        self.assertNotIn("Output names validated completed", events)
        self.assertNotIn("Batch completed", events)

    def test_recoverable_failure_logs_warning_and_continues(self):
        self.save_mut(self.mut_frame(quantity=-1), name="a_bad.parquet")
        self.save_mut(name="b_good.parquet")
        result = self.elt.run()
        self.assertEqual((result.failed, result.processed), (1, 1))
        events = self.events()
        self.assertIn("WARNING", events)
        self.assertIn("Rejected export; batch continues", events)
        self.assertNotIn("ERROR", events)
        self.assertNotIn("Input cleaned [Data/RAW/MUT_SALES/a_bad.parquet] completed", events)
        self.assertIn("Processed export: Data/RAW/MUT_SALES/b_good.parquet", events)
        self.assertIn("Batch completed", events)

    def test_quality_warnings_still_allow_success_milestones(self):
        self.save_mut(pl.concat([self.mut_frame()] * 2))
        self.assertEqual(self.elt.run().processed, 1)
        events = self.events()
        self.assertIn('"code": "repeated_lines_preserved"', events)
        self.assertIn("WARNING", events)
        self.assertIn("Parquet published [Data/RAW/MUT_SALES/orders.parquet] completed", events)
        self.assertIn("Processed export:", events)
        self.assertNotIn("ERROR", events)

    def test_fatal_write_logs_error_stops_and_omits_success_milestones(self):
        self.save_mut(name="a_first.parquet")
        self.save_mut(name="b_later.parquet")
        with patch.object(ELT, "_write_parquet", side_effect=OSError("Disk full")):
            with self.assertRaises(ELTError):
                self.elt.run()
        events = self.events()
        self.assertIn("ERROR", events)
        self.assertIn("Disk full", events)
        self.assertIn("Input loaded [Data/RAW/MUT_SALES/a_first.parquet] completed", events)
        self.assertNotIn("Parquet published [Data/RAW/MUT_SALES/a_first.parquet] completed", events)
        self.assertNotIn("Processed export:", events)
        self.assertNotIn("b_later.parquet", events)
        self.assertNotIn("Batch completed", events)
        self.assertEqual(next(iter(self.log()["files"].values()))["severity"], "ERROR")
        self.assertFalse(self.elt.lock_path.exists())

    def test_state_write_failure_is_fatal(self):
        self.save_mut()
        with patch("src.old_elt._atomic_json", side_effect=OSError("State is read-only")):
            with self.assertRaises(ELTError):
                self.elt.run()
        events = self.events()
        self.assertIn("ERROR", events)
        self.assertIn("State is read-only", events)
        self.assertNotIn("Processed export:", events)
        self.assertNotIn("Batch completed", events)

    def test_unexpected_processing_exception_is_fatal(self):
        self.save_mut()
        with patch.object(ELT, "clean_mut_sales", side_effect=TypeError("Unexpected bug")):
            with self.assertRaises(ELTError):
                self.elt.run()
        self.assertIn("ERROR", self.events())
        self.assertNotIn("Input cleaned [Data/RAW/MUT_SALES/orders.parquet] completed", self.events())

    def test_main_returns_zero_for_warnings_and_one_for_fatal_failure(self):
        self.save_mut(self.mut_frame(quantity=-1))
        with patch("sys.stderr", new_callable=io.StringIO) as console:
            self.assertEqual(main(self.root), 0)
            self.elt.log_path.write_text("broken history", encoding="utf-8")
            self.assertEqual(main(self.root), 1)
        self.assertIn("ERROR", console.getvalue())
        self.assertIn("ERROR", self.events())

    def test_injected_logger_gets_events_without_duplicate_handlers(self):
        self.save_mut()
        logger = logging.getLogger(f"test.elt.{self.root.name}")
        handlers = list(logger.handlers)
        instance = ELT(self.root, logger=logger)
        instance.run()
        instance.run()
        self.assertEqual(logger.handlers, handlers)
        events = self.events()
        self.assertEqual(events.count("Batch started;"), 2)
        self.assertEqual(events.count("Batch completed:"), 2)

    def test_pipeline_does_not_require_pandas_or_pyarrow(self):
        self.save_mut()
        repository = Path(__file__).resolve().parents[1]
        program = '''import builtins, sys
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'pandas', 'pyarrow'}:
        raise ImportError('Forbidden dependency: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
from src.old_elt import ELT
assert ELT(sys.argv[1]).run().processed == 1
'''
        completed = subprocess.run([sys.executable, "-c", program, str(self.root)], cwd=repository,
                                   capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
