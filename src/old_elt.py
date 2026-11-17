"""Validate RAW exports and publish typed Parquet tables in DEL.

Importing this module has no side effects. Call ``ELT().run()`` from the
application, or run this file to process all pending exports once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Iterator

import pandas as pd
from openpyxl import load_workbook
from pandas.testing import assert_frame_equal


LOGGER = logging.getLogger(__name__)
PIPELINE_VERSION = "1"
SOURCES = ("FRANKLIN_SALES", "FRANKLIN_STORE", "MUT_SALES", "MUT_STORE")
EXTENSIONS = {".xlsx", ".xlsm", ".parquet"}
PROVENANCE_COLUMNS = {"source", "source_file", "source_sheet", "source_row", "source_sha256"}

# Translate labels only. Business values, including product names, stay intact.
COLUMN_NAMES = {
    "fecha": "date", "hora": "time", "categoria": "category",
    "sub categoria": "subcategory", "producto": "product_name",
    "cantidades vendidas": "quantity", "cantidad": "quantity",
    "movimiento": "movement", "documento": "document", "folio": "document_number",
    "cliente": "customer", "canal": "channel", "variante / presentacion": "variant",
    "correlativo": "order_reference", "#orden": "order_id", "consumidor": "consumer",
    "correo": "email", "telefono": "phone", "tipo de documento": "document_type",
    "comercio": "merchant", "estado": "status", "moneda": "currency",
    "monto total": "total_amount", "precio unitario": "unit_price",
    "monto productos": "products_amount", "monto armados": "assemblies_amount",
    "medio de pago": "payment_method", "referencia de pago": "payment_reference",
    "entrega": "delivery_method", "tiempo entrega": "delivery_time_description",
    "fecha entrega": "delivery_date", "hora entrega": "delivery_time",
    "zona de entrega/ direccion": "delivery_zone_or_address", "sku producto": "product_sku",
    "observacion": "notes", "codigo de cupon": "coupon_code",
    "descuento del cupon": "coupon_discount", "descuento aplicado": "applied_discount",
    "modificadores": "modifiers", "totem": "totem",
    "ingresos totales $": "total_revenue", "% del total de ingresos": "revenue_percentage",
}
COMMON_REQUIRED = {"date", "category", "product_name", "quantity"}
REQUIRED = {
    "FRANKLIN_SALES": COMMON_REQUIRED | {"subcategory"},
    "FRANKLIN_STORE": COMMON_REQUIRED | {"movement", "document", "document_number", "variant"},
    "MUT_SALES": COMMON_REQUIRED | {"order_reference", "order_id", "product_sku", "status"},
}
REQUIRED["MUT_STORE"] = REQUIRED["MUT_SALES"]
NUMERIC_COLUMNS = {
    "total_amount", "unit_price", "products_amount", "assemblies_amount",
    "applied_discount", "total_revenue", "revenue_percentage",
}


class IntegrityError(ValueError):
    """An export cannot be published without losing or misinterpreting data."""


@dataclass
class FileResult:
    source_file: str
    status: str
    output: str | None = None
    rows: int = 0
    warnings: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class RunResult:
    files: list[FileResult] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return sum(item.status == "processed" for item in self.files)

    @property
    def skipped(self) -> int:
        return sum(item.status == "skipped" for item in self.files)

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.files)


def _normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).casefold().split())


def _column_name(value: Any, position: int) -> str:
    if value is None or str(value).strip() == "":
        return f"source_unlabeled_{position + 1}"
    label = _normalized(str(value))
    name = COLUMN_NAMES.get(label, re.sub(r"[^a-z0-9]+", "_", label).strip("_"))
    if not name:
        raise IntegrityError(f"Column {position + 1} has an unsupported label.")
    return name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ELT:
    """Process repository exports without modifying RAW or guessing corrections.

    Paths default to the repository containing this module, regardless of the
    working directory. The first successful batch establishes exact product
    names per source in ``elt_log.json``. Subsequent additions are reported for
    review. Names and repeated transaction lines are not automatically changed.
    """

    def __init__(self, repository_root: str | Path | None = None) -> None:
        self.root = (Path(repository_root) if repository_root is not None
                     else Path(__file__).resolve().parents[1]).resolve()
        self.raw_dir = self.root / "Data" / "RAW"
        self.del_dir = self.root / "Data" / "DEL"
        self.log_path = self.root / "elt_log.json"
        self.lock_path = self.root / ".elt.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError("Another ELT run holds .elt.lock. If it crashed, remove the stale lock.") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(f"pid={os.getpid()} started_at={_utc_now()}\n")
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    def _read_log(self) -> dict[str, Any]:
        if not self.log_path.exists():
            return {"log_version": 1, "product_catalog": {}, "files": {}}
        try:
            log = json.loads(self.log_path.read_text(encoding="utf-8"))
            if (log["log_version"] != 1 or not isinstance(log["files"], dict)
                    or not isinstance(log["product_catalog"], dict)):
                raise ValueError("Unsupported log structure")
            for names in log["product_catalog"].values():
                if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                    raise ValueError("Invalid product catalog")
            if not all(isinstance(entry, dict) for entry in log["files"].values()):
                raise ValueError("Invalid file records")
            return log
        except (ValueError, KeyError, TypeError) as exc:
            raise IntegrityError("elt_log.json is invalid; restore or repair it before running ELT.") from exc

    def discover_files(self) -> list[tuple[str, Path]]:
        """Find supported files recursively; ignore Excel lock files and hidden files."""
        if not self.raw_dir.is_dir():
            raise FileNotFoundError(f"RAW directory does not exist: {self.raw_dir}")
        unknown = [p.name for p in self.raw_dir.iterdir()
                   if p.is_dir() and p.name not in SOURCES and not p.name.startswith(".")]
        if unknown:
            raise IntegrityError(f"Unknown RAW source directories: {', '.join(sorted(unknown))}")
        files = []
        for source in SOURCES:
            directory = self.raw_dir / source
            for path in sorted(directory.rglob("*")) if directory.exists() else []:
                relative = path.relative_to(directory)
                if (path.is_file() and path.suffix.lower() in EXTENSIONS
                        and not any(part.startswith(("~$", ".")) for part in relative.parts)):
                    if not path.resolve().is_relative_to(self.raw_dir.resolve()):
                        raise IntegrityError("RAW files must resolve inside Data/RAW.")
                    files.append((source, path))
        return files

    @staticmethod
    def _read_sheet(workbook: Any, sheet: str, required: set[str]) -> pd.DataFrame:
        if sheet not in workbook.sheetnames:
            raise IntegrityError(f"Missing worksheet: {sheet}")
        rows = workbook[sheet].iter_rows()
        for number, cells in enumerate(rows, start=1):
            columns = [_column_name(cell.value, i) for i, cell in enumerate(cells)]
            if required.issubset(columns):
                break
            if number >= 20:
                raise IntegrityError(f"Required headers not found in the first 20 rows of {sheet}.")
        else:
            raise IntegrityError(f"Required headers not found in {sheet}.")
        if len(set(columns)) != len(columns):
            raise IntegrityError(f"Duplicate column labels in {sheet}.")
        if PROVENANCE_COLUMNS.intersection(columns):
            raise IntegrityError("Input columns conflict with reserved provenance columns.")
        values, row_numbers = [], []
        for row_number, cells in enumerate(rows, start=number + 1):
            if any(cell.data_type == "e" for cell in cells):
                raise IntegrityError(f"Excel error in {sheet}, row {row_number}.")
            values.append([cell.value for cell in cells])
            row_numbers.append(row_number)
        frame = pd.DataFrame(values, columns=columns)
        frame.index = pd.Index(row_numbers, name="source_row")
        return frame

    def _load(self, path: Path, source: str) -> tuple[pd.DataFrame, str, dict[str, Any]]:
        if path.suffix.lower() == ".parquet":
            frame = pd.read_parquet(path, engine="pyarrow")
            columns = [_column_name(c, i) for i, c in enumerate(frame.columns)]
            if len(set(columns)) != len(columns) or PROVENANCE_COLUMNS.intersection(columns):
                raise IntegrityError("Duplicate or reserved column labels in RAW Parquet.")
            frame.columns = columns
            frame.index = pd.RangeIndex(1, len(frame) + 1, name="source_row")
            return frame, "parquet", {}
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = {"FRANKLIN_SALES": "Detalle", "FRANKLIN_STORE": "Detalle de ventas"}.get(source, "Sheet1")
            frame = self._read_sheet(workbook, sheet, REQUIRED[source])
            controls: dict[str, Any] = {}
            if source == "FRANKLIN_SALES" and "Ranking del período" in workbook.sheetnames:
                controls["ranking"] = self._read_sheet(
                    workbook, "Ranking del período", {"category", "subcategory", "product_name", "quantity"})
            if source == "FRANKLIN_STORE" and "Resumen" in workbook.sheetnames:
                summary, errors = {}, 0
                for cells in workbook["Resumen"].iter_rows():
                    errors += sum(cell.data_type == "e" for cell in cells)
                    if len(cells) >= 2 and isinstance(cells[0].value, str):
                        summary[_normalized(cells[0].value)] = cells[1].value
                controls.update(summary=summary, summary_errors=errors)
            return frame, sheet, controls
        finally:
            workbook.close()

    @staticmethod
    def _require_values(frame: pd.DataFrame, columns: set[str]) -> None:
        for column in sorted(columns):
            invalid = frame[column].isna() | frame[column].map(
                lambda value: isinstance(value, str) and not value.strip())
            if invalid.any():
                raise IntegrityError(f"Missing {column} at source rows {frame.index[invalid].tolist()[:10]}.")

    @staticmethod
    def _parse_dates(series: pd.Series, *, optional: bool = False) -> pd.Series:
        def parse(value: Any) -> pd.Timestamp:
            if pd.isna(value) or value == "":
                if optional:
                    return pd.NaT
                raise ValueError("Missing date")
            if isinstance(value, (int, float, bool)):
                raise ValueError("Unformatted numeric date")
            if isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value)
                except ValueError:
                    for date_format in ("%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
                        try:
                            value = datetime.strptime(value, date_format)
                            break
                        except ValueError:
                            continue
                    else:
                        raise ValueError("Unrecognized date format")
            timestamp = pd.Timestamp(value)
            if timestamp.tzinfo is not None or timestamp != timestamp.normalize():
                raise ValueError("Expected a date without time or timezone")
            return timestamp
        try:
            return pd.to_datetime(series.map(parse)).astype("datetime64[ns]")
        except (ValueError, TypeError, OverflowError) as exc:
            raise IntegrityError(f"Invalid dates in {series.name}.") from exc

    @staticmethod
    def _parse_numbers(series: pd.Series, *, integer: bool = False) -> pd.Series:
        try:
            if series.map(lambda v: isinstance(v, bool)).any():
                raise ValueError("Boolean numeric value")
            values = pd.to_numeric(series, errors="raise")
            if values.isna().any() or not values.map(math.isfinite).all():
                raise ValueError("Missing or non-finite numeric value")
            if integer and ((values % 1 != 0).any() or (values.abs() >= 2**63).any()):
                raise ValueError("Expected signed 64-bit integer units")
            return values.astype("int64" if integer else "float64")
        except (ValueError, TypeError, OverflowError) as exc:
            raise IntegrityError(f"Invalid numeric values in {series.name}.") from exc

    def _clean_common(self, frame: pd.DataFrame, source: str) -> pd.DataFrame:
        missing = REQUIRED[source] - set(frame.columns)
        if missing:
            raise IntegrityError(f"Missing required columns: {', '.join(sorted(missing))}")
        cleaned = frame.copy(deep=True)
        blank = cleaned.map(lambda v: v is None or (isinstance(v, str) and not v.strip()) or pd.isna(v))
        cleaned = cleaned.loc[~blank.all(axis=1)].copy()
        for column in list(cleaned.columns):
            if column.startswith("source_unlabeled_") and blank.loc[cleaned.index, column].all():
                cleaned = cleaned.drop(columns=column)
        if cleaned.empty:
            raise IntegrityError("The detail table contains no data rows.")
        self._require_values(cleaned, REQUIRED[source] - {"variant"})
        for column in ("category", "subcategory", "product_name", "variant"):
            if column in cleaned and not cleaned[column].dropna().map(lambda v: isinstance(v, str)).all():
                raise IntegrityError(f"Non-text values in {column}.")
        cleaned["date"] = self._parse_dates(cleaned["date"])
        cleaned["quantity"] = self._parse_numbers(cleaned["quantity"], integer=True)
        if (cleaned["quantity"] == 0).any():
            raise IntegrityError("Zero quantities are not valid sales or credit-note lines.")
        for column in cleaned.columns:
            if column in {"date", "quantity"}:
                continue
            if column == "delivery_date":
                cleaned[column] = self._parse_dates(cleaned[column], optional=True)
            elif column in NUMERIC_COLUMNS:
                populated = cleaned[column].notna() & cleaned[column].ne("")
                numbers = pd.Series(float("nan"), index=cleaned.index, dtype="float64")
                numbers.loc[populated] = self._parse_numbers(cleaned.loc[populated, column])
                cleaned[column] = numbers
            else:
                cleaned[column] = cleaned[column].astype("string")
        return cleaned

    def clean_franklin_sales(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Validate the daily product/category export; retain every original name."""
        cleaned = self._clean_common(frame, "FRANKLIN_SALES")
        if (cleaned["quantity"] < 0).any():
            raise IntegrityError("FRANKLIN_SALES daily quantities must be positive.")
        if cleaned.duplicated(["date", "category", "subcategory", "product_name"]).any():
            raise IntegrityError("Duplicate daily product keys in FRANKLIN_SALES; review the export.")
        return cleaned

    def clean_franklin_store(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Retain variants and credit notes, checking movement/document signs."""
        cleaned = self._clean_common(frame, "FRANKLIN_STORE")
        movement = cleaned["movement"].map(_normalized)
        document = cleaned["document"].map(_normalized)
        sale, credit = movement.eq("venta"), movement.eq("nota de credito")
        if not (sale | credit).all():
            raise IntegrityError("Unrecognized FRANKLIN_STORE movement.")
        if ((sale & cleaned["quantity"].lt(0)) | (credit & cleaned["quantity"].gt(0))).any():
            raise IntegrityError("Movement and quantity signs disagree in FRANKLIN_STORE.")
        if not document.str.contains("nota de credito", regex=False).eq(credit).all():
            raise IntegrityError("Movement and document type disagree in FRANKLIN_STORE.")
        return cleaned

    def clean_mut_sales(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Support both MUT layouts, retaining repeated lines and optional amounts."""
        cleaned = self._clean_common(frame, "MUT_SALES")
        if (cleaned["quantity"] < 0).any():
            raise IntegrityError("MUT quantities must be positive; refunds need an explicit rule.")
        for column in ("time", "delivery_time"):
            if column in cleaned:
                populated = cleaned[column].notna() & cleaned[column].ne("")
                if not cleaned.loc[populated, column].str.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d").all():
                    raise IntegrityError(f"Invalid HH:MM:SS values in {column}.")
        if "currency" in cleaned and not cleaned["currency"].dropna().eq("CLP").all():
            raise IntegrityError("Unexpected MUT currency; amount conversion is not configured.")
        return cleaned

    def clean_mut_store(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Accept MUT_STORE as an alias for the repository's MUT_SALES layout."""
        return self.clean_mut_sales(frame)

    @staticmethod
    def _warning(code: str, count: int, **details: Any) -> dict[str, Any]:
        return {"code": code, "count": int(count), **details}

    def _audit(self, original: pd.DataFrame, cleaned: pd.DataFrame, source: str,
               controls: dict[str, Any], baseline: list[str] | None) -> dict[str, Any]:
        warnings = []
        duplicates = int(cleaned.duplicated().sum())
        if duplicates:
            warnings.append(self._warning("repeated_lines_preserved", duplicates))
        text = cleaned.select_dtypes(include=["string", "object"])
        whitespace = sum(isinstance(value, str) and value != value.strip()
                         for column in text for value in text[column].dropna())
        if whitespace:
            warnings.append(self._warning("surrounding_whitespace_preserved", whitespace))
        unlabeled = [c for c in cleaned if c.startswith("source_unlabeled_")]
        if unlabeled:
            warnings.append(self._warning("unlabeled_columns_preserved", len(unlabeled), columns=unlabeled))
        today = pd.Timestamp.now(tz="America/Santiago").tz_localize(None).normalize()
        future = int(cleaned["date"].gt(today).sum())
        if future:
            warnings.append(self._warning("future_dates", future))
        if source.startswith("MUT"):
            unpaid = int(cleaned["status"].map(_normalized).ne("pagado").sum())
            if unpaid:
                warnings.append(self._warning("non_paid_lines_preserved", unpaid))
            for identifier in ("order_reference", "order_id"):
                conflicts = cleaned.groupby(identifier).agg(date_count=("date", "nunique"),
                                                           status_count=("status", "nunique"))
                count = int((conflicts > 1).any(axis=1).sum())
                if count:
                    warnings.append(self._warning(f"inconsistent_{identifier}", count))
        names = sorted(cleaned["product_name"].unique().tolist())
        if baseline is not None:
            new_names = sorted(set(names) - set(baseline))
            if new_names:
                candidates = {}
                normalized_baseline = {_normalized(n): n for n in baseline}
                for name in new_names:
                    matches = get_close_matches(_normalized(name), normalized_baseline, n=3, cutoff=0.85)
                    if matches:
                        candidates[name] = [normalized_baseline[m] for m in matches]
                warnings.append(self._warning("unrecognized_product_names", len(new_names),
                                              names=new_names, possible_matches=candidates))
        ranking = controls.get("ranking")
        if ranking is not None:
            ranking = ranking.dropna(how="all").copy()
            self._require_values(ranking, {"category", "subcategory", "product_name", "quantity"})
            ranking["quantity"] = self._parse_numbers(ranking["quantity"], integer=True)
            key = ["category", "subcategory", "product_name"]
            if ranking.duplicated(key).any():
                raise IntegrityError("Duplicate products in the Franklin ranking control.")
            detail_units = cleaned.groupby(key, dropna=False)["quantity"].sum()
            ranking_units = ranking.groupby(key, dropna=False)["quantity"].sum()
            if detail_units.subtract(ranking_units, fill_value=0).ne(0).any():
                raise IntegrityError("Franklin detail quantities do not reconcile to the ranking.")
        elif source == "FRANKLIN_SALES":
            warnings.append(self._warning("ranking_control_unavailable", 1))
        summary = controls.get("summary", {})
        if source == "FRANKLIN_STORE":
            for label, actual in (
                ("filas de detalle", len(cleaned)),
                ("documentos de venta", cleaned.loc[cleaned["quantity"].gt(0),
                                                   ["document", "document_number"]].drop_duplicates().shape[0]),
                ("documentos de nc", cleaned.loc[cleaned["quantity"].lt(0),
                                                ["document", "document_number"]].drop_duplicates().shape[0]),
            ):
                if label in summary and summary[label] != actual:
                    raise IntegrityError(f"Store summary does not reconcile: {label}.")
            if not summary:
                warnings.append(self._warning("store_summary_unavailable", 1))
        if controls.get("summary_errors"):
            warnings.append(self._warning("excel_errors_in_auxiliary_summary", controls["summary_errors"]))
        dates = cleaned["date"].drop_duplicates().sort_values()
        missing_days = int((dates.max() - dates.min()).days + 1 - len(dates))
        if missing_days:
            warnings.append(self._warning("days_without_observations", missing_days))
        if len(cleaned) > len(original):
            raise IntegrityError("Cleaning unexpectedly added rows.")
        if not original.loc[cleaned.index, "product_name"].astype("string").equals(cleaned["product_name"]):
            raise IntegrityError("Cleaning unexpectedly changed product names.")
        original_quantities = self._parse_numbers(original.loc[cleaned.index, "quantity"], integer=True)
        if not original_quantities.equals(cleaned["quantity"]):
            raise IntegrityError("Cleaning unexpectedly changed quantities.")
        return {
            "input_rows": len(original), "output_rows": len(cleaned),
            "empty_rows_removed": len(original) - len(cleaned),
            "quantity_total": int(cleaned["quantity"].sum()),
            "date_min": dates.min().date().isoformat(), "date_max": dates.max().date().isoformat(),
            "null_counts": {c: int(n) for c, n in cleaned.isna().sum().items()},
            "product_names": names, "warnings": warnings,
        }

    def _valid_output(self, record: dict[str, Any]) -> bool:
        if record.get("status") != "processed" or record.get("pipeline_version") != PIPELINE_VERSION:
            return False
        output = record.get("output")
        if not isinstance(output, str):
            return False
        path = (self.root / output).resolve()
        return (path.is_relative_to(self.del_dir.resolve()) and path.is_file()
                and _sha256(path) == record.get("output_sha256"))

    @staticmethod
    def _write_parquet(frame: pd.DataFrame, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            frame.to_parquet(temporary, engine="pyarrow", compression="snappy", index=False)
            restored = pd.read_parquet(temporary, engine="pyarrow")
            assert_frame_equal(frame.reset_index(drop=True), restored, check_dtype=False, check_exact=True)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)

    def run(self) -> RunResult:
        """Process pending files, isolate failures, and persist progress after each file.

        Source hashes are skipped only while their versioned, verified DEL
        artifact exists. Changed RAW files replace their previous artifact;
        missing or altered artifacts are rebuilt. Identical copies within one
        source reuse an artifact, avoiding double ingestion.
        """
        result = RunResult()
        with self._locked():
            files = self.discover_files()
            log = self._read_log()
            for source, path in files:
                relative = path.relative_to(self.root).as_posix()
                previous = log["files"].get(relative, {})
                try:
                    digest = _sha256(path)
                    matching = next((entry for entry in [previous, *log["files"].values()]
                                     if entry.get("source") == source and entry.get("source_sha256") == digest
                                     and self._valid_output(entry)), None)
                    if matching is not None:
                        log["files"][relative] = dict(matching)
                        result.files.append(FileResult(relative, "skipped", matching["output"],
                                                       matching["report"]["output_rows"],
                                                       matching["report"]["warnings"]))
                        _atomic_json(self.log_path, log)
                        LOGGER.info("Skipped %s", relative)
                        continue
                    frame, sheet, controls = self._load(path, source)
                    cleaned = getattr(self, f"clean_{source.lower()}")(frame)
                    report = self._audit(frame, cleaned, source, controls, log["product_catalog"].get(source))
                    for other_path, entry in log["files"].items():
                        if (other_path != relative and entry.get("source") == source
                                and entry.get("status") == "processed" and "report" in entry
                                and entry["report"]["date_min"] <= report["date_max"]
                                and entry["report"]["date_max"] >= report["date_min"]):
                            report["warnings"].append(self._warning("overlapping_export_period", 1,
                                                                  other_file=other_path))
                    cleaned["source"] = source
                    cleaned["source_file"] = relative
                    cleaned["source_sheet"] = sheet
                    cleaned["source_row"] = cleaned.index.astype("int64")
                    cleaned["source_sha256"] = digest
                    if _sha256(path) != digest:
                        raise IntegrityError("RAW file changed during processing; retry after it is saved.")
                    output = self.del_dir / path.relative_to(self.raw_dir).with_suffix(path.suffix.lower() + ".parquet")
                    self._write_parquet(cleaned, output)
                    record = {
                        "status": "processed", "source": source, "source_sha256": digest,
                        "pipeline_version": PIPELINE_VERSION, "processed_at": _utc_now(),
                        "output": output.relative_to(self.root).as_posix(),
                        "output_sha256": _sha256(output), "report": report,
                    }
                    log["files"][relative] = record
                    result.files.append(FileResult(relative, "processed", record["output"],
                                                   len(cleaned), report["warnings"]))
                    LOGGER.info("Processed %s: %d rows, %d warnings", relative, len(cleaned), len(report["warnings"]))
                except Exception as exc:
                    # Retain the last accepted output if a changed source fails validation.
                    log["files"][relative] = {
                        "status": "failed", "source": source, "failed_at": _utc_now(), "error": str(exc),
                        "last_success": previous if previous.get("status") == "processed"
                        else previous.get("last_success"),
                    }
                    result.files.append(FileResult(relative, "failed", error=str(exc)))
                    LOGGER.error("Failed %s: %s", relative, exc)
                _atomic_json(self.log_path, log)
            # Bootstrap from all successful current exports, not only the first file.
            for source in SOURCES:
                source_failed = any(item.status == "failed" and
                                    Path(item.source_file).relative_to("Data/RAW").parts[0] == source
                                    for item in result.files)
                if source not in log["product_catalog"] and not source_failed:
                    names = {name for entry in log["files"].values()
                             if entry.get("source") == source and entry.get("status") == "processed"
                             for name in entry["report"]["product_names"]}
                    if names:
                        log["product_catalog"][source] = sorted(names)
            log["last_run_at"] = _utc_now()
            log["last_run"] = {"processed": result.processed, "skipped": result.skipped, "failed": result.failed}
            _atomic_json(self.log_path, log)
        return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        outcome = ELT().run()
    except (OSError, IntegrityError, RuntimeError) as error:
        LOGGER.error("ELT could not start: %s", error)
        raise SystemExit(1) from error
    LOGGER.info("Finished: %d processed, %d skipped, %d failed",
                outcome.processed, outcome.skipped, outcome.failed)
    raise SystemExit(1 if outcome.failed else 0)
