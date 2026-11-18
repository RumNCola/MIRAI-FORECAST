"""Validate RAW exports with Polars and publish Parquet tables in DEL.

Importing this module has no side effects. Use ``ELT().run()`` from the
application, or execute this file for a standalone batch. Recoverable input
failures are warnings; fatal failures are logged and raised as ``ELTError``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Iterator
from zipfile import BadZipFile
from zoneinfo import ZoneInfo

import polars as pl
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from polars.testing import assert_frame_equal


LOGGER = logging.getLogger(__name__)
PIPELINE_VERSION = "2"
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
TEXT_COLUMNS = {"category", "subcategory", "product_name", "variant"}


class IntegrityError(ValueError):
    """Reject an individual input and continue the batch with a WARNING."""


class ELTError(RuntimeError):
    """Stop the batch because safe publication or execution is no longer possible."""


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
        """Count inputs rejected with warnings; fatal failures raise ELTError."""
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
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise OSError(f"Cannot hash {path}: {exc}") from exc
    else:
        return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remove_temporary(path: Path, logger: logging.Logger) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove temporary file %s; execution continues", path, exc_info=True)


def _atomic_json(path: Path, value: dict[str, Any], logger: logging.Logger = LOGGER) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception as exc:
        raise ELTError(f"Cannot save processing state to {path}: {exc}") from exc
    finally:
        if temporary is not None:
            _remove_temporary(temporary, logger)


class _StrictFileHandler(logging.FileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # The pipeline cannot claim success if its audit trail cannot be written.
        raise ELTError("Cannot write to elt.log") from sys.exception()


@contextmanager
def event_logger(root: Path, logger: logging.Logger | None = None,
                 *, console: bool = False) -> Iterator[logging.Logger]:
    """Attach an append-only event log for this run, then release its file handle."""
    logger = logger or logging.getLogger(f"mirai.elt.{hashlib.sha256(str(root).encode()).hexdigest()[:12]}")
    owned: list[logging.Handler] = []
    old_level, old_propagate = logger.level, logger.propagate
    try:
        logger.setLevel(logging.INFO)
        logger.propagate = False
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        if console:
            handler = logging.StreamHandler()
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            owned.append(handler)
        log_path = (root / "elt.log").resolve()
        if not any(isinstance(h, logging.FileHandler) and Path(h.baseFilename) == log_path
                   for h in logger.handlers):
            handler = _StrictFileHandler(log_path, encoding="utf-8")
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            owned.append(handler)
    except Exception as exc:
        for handler in owned:
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(old_level)
        logger.propagate = old_propagate
        raise ELTError(f"Cannot initialize event logging in {root}: {exc}") from exc
    try:
        yield logger
    finally:
        for handler in owned:
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(old_level)
        logger.propagate = old_propagate


class ELT:
    """Process repository exports without changing names or deleting repeated lines.

    Every run writes INFO milestones, WARNING recoveries and fatal ERRORs to
    ``elt.log``. ``elt_log.json`` retains hashes, product catalogs and file state.
    An injected logger is supported; the event file remains attached during run.
    """

    def __init__(self, repository_root: str | Path | None = None,
                 *, logger: logging.Logger | None = None) -> None:
        try:
            self.root = (Path(repository_root) if repository_root is not None
                         else Path(__file__).resolve().parents[1]).resolve()
            self.raw_dir = self.root / "Data" / "RAW"
            self.del_dir = self.root / "Data" / "DEL"
            self.log_path = self.root / "elt_log.json"
            self.lock_path = self.root / ".elt.lock"
            self.logger = logger or logging.getLogger(
                f"mirai.elt.{hashlib.sha256(str(self.root).encode()).hexdigest()[:12]}")
        except (OSError, TypeError, ValueError) as exc:
            raise ELTError(f"Cannot initialize repository paths: {exc}") from exc

    @contextmanager
    def _stage(self, name: str, *, input_stage: bool = False) -> Iterator[None]:
        """Catch stage failures and emit a milestone only from the successful else."""
        try:
            yield
        except ELTError:
            raise
        except IntegrityError as exc:
            if input_stage:
                raise
            raise ELTError(f"{name}: {exc}") from exc
        except (OSError, ValueError, BadZipFile, InvalidFileException, pl.exceptions.PolarsError) as exc:
            exception_type = IntegrityError if input_stage else ELTError
            raise exception_type(f"{name}: {exc}") from exc
        except Exception as exc:
            raise ELTError(f"{name}: unexpected {type(exc).__name__}: {exc}") from exc
        else:
            self.logger.info("%s completed", name)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ELTError("Another ELT run holds .elt.lock. If it crashed, remove the stale lock.") from exc
        except OSError as exc:
            raise ELTError(f"Cannot acquire the execution lock: {exc}") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(f"pid={os.getpid()} started_at={_utc_now()}\n")
            self.logger.info("Execution lock acquired")
            yield
        finally:
            try:
                self.lock_path.unlink(missing_ok=True)
            except OSError:
                self.logger.warning("Could not release .elt.lock; execution continues", exc_info=True)
            else:
                self.logger.info("Execution lock released")

    def _read_log(self) -> dict[str, Any]:
        try:
            if not self.log_path.exists():
                return {"log_version": 1, "product_catalog": {}, "files": {}}
            log = json.loads(self.log_path.read_text(encoding="utf-8"))
            if (log["log_version"] != 1 or not isinstance(log["files"], dict)
                    or not isinstance(log["product_catalog"], dict)):
                raise ValueError("Unsupported log structure")
            for names in log["product_catalog"].values():
                if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                    raise ValueError("Invalid product catalog")
            for entry in log["files"].values():
                if not isinstance(entry, dict):
                    raise ValueError("Invalid file record")
                if entry.get("status") == "processed":
                    report = entry["report"]
                    if (not isinstance(report["output_rows"], int)
                            or not isinstance(report["product_names"], list)
                            or not isinstance(report["warnings"], list)):
                        raise ValueError("Invalid successful-file report")
                    date.fromisoformat(report["date_min"])
                    date.fromisoformat(report["date_max"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ELTError("elt_log.json is unreadable or invalid; restore or repair it before running ELT.") from exc
        else:
            return log

    def discover_files(self) -> list[tuple[str, Path]]:
        try:
            if not self.raw_dir.is_dir():
                raise ELTError(f"RAW directory does not exist: {self.raw_dir}")
            unknown = [p.name for p in self.raw_dir.iterdir()
                       if p.is_dir() and p.name not in SOURCES and not p.name.startswith(".")]
            if unknown:
                raise ELTError(f"Unknown RAW source directories: {', '.join(sorted(unknown))}")
            loose = sorted(p.name for p in self.raw_dir.iterdir()
                           if p.is_file() and p.suffix.lower() in EXTENSIONS and not p.name.startswith(("~$", ".")))
            if loose:
                # Files directly in Data/RAW are never read; say where they belong instead of failing later.
                raise ELTError(
                    "Hay archivos sueltos en Data/RAW que no se procesan: " + "; ".join(loose) + ". "
                    "Mueve cada Excel a la carpeta de su fuente: Data/RAW/MUT_SALES (ventas MUT), "
                    "Data/RAW/FRANKLIN_SALES (ventas Franklin), Data/RAW/FRANKLIN_STORE (tienda Franklin) "
                    "o Data/RAW/MUT_STORE.")
            files = []
            for source in SOURCES:
                directory = self.raw_dir / source
                for path in sorted(directory.rglob("*")) if directory.exists() else []:
                    relative = path.relative_to(directory)
                    if (path.is_file() and path.suffix.lower() in EXTENSIONS
                            and not any(part.startswith(("~$", ".")) for part in relative.parts)):
                        if not path.resolve().is_relative_to(self.raw_dir.resolve()):
                            raise ELTError("RAW files must resolve inside Data/RAW.")
                        files.append((source, path))
        except OSError as exc:
            raise ELTError(f"Cannot discover RAW exports: {exc}") from exc
        else:
            return files

    def _output_path(self, path: Path) -> Path:
        output = self.del_dir / path.relative_to(self.raw_dir).with_suffix(".parquet")
        if not output.resolve().is_relative_to(self.del_dir.resolve()):
            raise ELTError("Output paths must resolve inside Data/DEL.")
        return output

    def _check_output_collisions(self, files: list[tuple[str, Path]]) -> None:
        destinations: dict[str, Path] = {}
        for _, path in files:
            key = os.path.normcase(str(self._output_path(path).resolve()))
            if key in destinations:
                raise ELTError(f"Output name collision: {destinations[key].name} and {path.name}. "
                               "Rename one input or place it in a separate subdirectory.")
            destinations[key] = path

    @staticmethod
    def _read_sheet(workbook: Any, sheet: str, required: set[str]) -> pl.DataFrame:
        try:
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
            if len(set(columns)) != len(columns) or PROVENANCE_COLUMNS.intersection(columns):
                raise IntegrityError(f"Duplicate or reserved column labels in {sheet}.")
            values, row_numbers = [], []
            for row_number, cells in enumerate(rows, start=number + 1):
                if any(cell.data_type == "e" for cell in cells):
                    raise IntegrityError(f"Excel error in {sheet}, row {row_number}.")
                values.append([cell.value for cell in cells])
                row_numbers.append(row_number)
            width = max(len(columns), max((len(row) for row in values), default=0))
            columns.extend(_column_name(None, i) for i in range(len(columns), width))
            if len(set(columns)) != len(columns):
                raise IntegrityError(f"Duplicate generated column labels in {sheet}.")
            values = [row + [None] * (width - len(row)) for row in values]
            series = []
            for position, column in enumerate(columns):
                cells = [row[position] for row in values]
                if column in TEXT_COLUMNS and any(v is not None and not isinstance(v, str) for v in cells):
                    raise IntegrityError(f"Non-text values in {column}.")
                if column in {"date", "delivery_date"}:
                    if any(v is not None and not isinstance(v, (str, date)) for v in cells):
                        raise IntegrityError(f"Invalid dates in {column}; numeric date serials are unsupported.")
                    cells = [v.isoformat() if isinstance(v, date) else v for v in cells]
                # Text staging preserves mixed identifiers and exact numeric literals.
                series.append(pl.Series(column, [None if v is None else str(v) for v in cells], dtype=pl.String))
            frame = pl.DataFrame(series).with_columns(pl.Series("source_row", row_numbers, dtype=pl.Int64))
        except (OSError, ValueError, pl.exceptions.PolarsError) as exc:
            raise IntegrityError(f"Cannot read worksheet {sheet}: {exc}") from exc
        else:
            return frame

    def _load(self, path: Path, source: str) -> tuple[pl.DataFrame, str, dict[str, Any]]:
        workbook = None
        try:
            if path.suffix.lower() == ".parquet":
                frame = pl.read_parquet(path)
                columns = [_column_name(c, i) for i, c in enumerate(frame.columns)]
                if len(set(columns)) != len(columns) or PROVENANCE_COLUMNS.intersection(columns):
                    raise IntegrityError("Duplicate or reserved column labels in RAW Parquet.")
                return frame.rename(dict(zip(frame.columns, columns))).with_row_index("source_row", offset=1), "parquet", {}
            workbook = load_workbook(path, read_only=True, data_only=True)
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
        except (OSError, ValueError, BadZipFile, InvalidFileException, pl.exceptions.PolarsError) as exc:
            raise IntegrityError(f"Cannot load {path.name}: {exc}") from exc
        finally:
            if workbook is not None:
                try:
                    workbook.close()
                except OSError:
                    self.logger.warning("Could not close workbook %s; execution continues", path, exc_info=True)

    @staticmethod
    def _with_source_rows(frame: pl.DataFrame) -> pl.DataFrame:
        return frame if "source_row" in frame.columns else frame.with_row_index("source_row", offset=1)

    @staticmethod
    def _blank(column: str, dtype: pl.DataType) -> pl.Expr:
        expression = pl.col(column).is_null()
        if dtype == pl.String:
            expression = expression | pl.col(column).str.strip_chars().eq("")
        elif dtype.is_float():
            expression = expression | pl.col(column).is_nan()
        return expression.fill_null(True)

    @classmethod
    def _drop_empty_rows(cls, frame: pl.DataFrame) -> pl.DataFrame:
        columns = [c for c in frame.columns if c != "source_row"]
        return frame.filter(~pl.all_horizontal([cls._blank(c, frame.schema[c]) for c in columns]))

    @staticmethod
    def _require_values(frame: pl.DataFrame, columns: set[str]) -> None:
        for column in sorted(columns):
            invalid = frame.filter(ELT._blank(column, frame.schema[column]))
            if invalid.height:
                rows = invalid["source_row"].head(10).to_list() if "source_row" in invalid.columns else []
                raise IntegrityError(f"Missing {column} at source rows {rows}.")

    @staticmethod
    def _parse_dates(series: pl.Series, *, optional: bool = False) -> pl.Series:
        try:
            parsed = []
            for value in series.to_list():
                if value is None or value == "":
                    if optional:
                        parsed.append(None)
                        continue
                    raise ValueError("Missing date")
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
                if isinstance(value, datetime):
                    if value.tzinfo is not None or value.time() != time.min:
                        raise ValueError("Expected a date without time or timezone")
                elif isinstance(value, date):
                    value = datetime.combine(value, time.min)
                else:
                    raise ValueError("Unformatted numeric or invalid date")
                parsed.append(value)
            result = pl.Series(series.name, parsed, dtype=pl.Datetime("us"))
        except (ValueError, TypeError, OverflowError, pl.exceptions.PolarsError) as exc:
            raise IntegrityError(f"Invalid dates in {series.name}: {exc}") from exc
        else:
            return result

    @staticmethod
    def _parse_numbers(series: pl.Series, *, integer: bool = False, optional: bool = False) -> pl.Series:
        try:
            values = []
            for value in series.to_list():
                if optional and (value is None or value == ""):
                    values.append(None)
                    continue
                if value is None or isinstance(value, bool):
                    raise ValueError("Missing or boolean numeric value")
                number = Decimal(str(value))
                if not number.is_finite():
                    raise ValueError("Non-finite numeric value")
                if integer and (number != number.to_integral_value() or not -(2**63) <= number < 2**63):
                    raise ValueError("Expected signed 64-bit integer units")
                values.append(int(number) if integer else float(number))
            result = pl.Series(series.name, values, dtype=pl.Int64 if integer else pl.Float64)
            if not integer and not result.drop_nulls().is_finite().all():
                raise ValueError("Numeric value exceeds float64 range")
        except (InvalidOperation, ValueError, TypeError, OverflowError, pl.exceptions.PolarsError) as exc:
            raise IntegrityError(f"Invalid numeric values in {series.name}: {exc}") from exc
        else:
            return result

    def _clean_common(self, frame: pl.DataFrame, source: str) -> pl.DataFrame:
        try:
            missing = REQUIRED[source] - set(frame.columns)
            if missing:
                raise IntegrityError(f"Missing required columns: {', '.join(sorted(missing))}")
            cleaned = self._drop_empty_rows(self._with_source_rows(frame))
            for column in list(cleaned.columns):
                if column.startswith("source_unlabeled_") and cleaned.select(
                        self._blank(column, cleaned.schema[column]).all()).item():
                    cleaned = cleaned.drop(column)
            if cleaned.is_empty():
                raise IntegrityError("The detail table contains no data rows.")
            self._require_values(cleaned, REQUIRED[source] - {"variant"})
            for column in TEXT_COLUMNS.intersection(cleaned.columns):
                if cleaned.schema[column].base_type() not in (pl.String, pl.Null, pl.Categorical, pl.Enum):
                    raise IntegrityError(f"Non-text values in {column}.")
            if (cleaned["source_row"].null_count() or cleaned["source_row"].n_unique() != cleaned.height
                    or not cleaned.schema["source_row"].is_integer() or cleaned["source_row"].min() < 1):
                raise IntegrityError("Invalid or duplicate physical source rows.")
            cleaned = cleaned.with_columns(self._parse_dates(cleaned["date"]),
                                           self._parse_numbers(cleaned["quantity"], integer=True))
            if cleaned["quantity"].eq(0).any():
                raise IntegrityError("Zero quantities are not valid sales or credit-note lines.")
            expressions = []
            for column in cleaned.columns:
                if column in {"date", "quantity"}:
                    continue
                if column == "source_row":
                    expressions.append(pl.col(column).cast(pl.Int64))
                elif column == "delivery_date":
                    expressions.append(self._parse_dates(cleaned[column], optional=True))
                elif column in NUMERIC_COLUMNS:
                    expressions.append(self._parse_numbers(cleaned[column], optional=True))
                else:
                    expressions.append(pl.col(column).cast(pl.String))
            return cleaned.with_columns(expressions)
        except pl.exceptions.PolarsError as exc:
            raise IntegrityError(f"Cannot clean {source}: {exc}") from exc

    def clean_franklin_sales(self, frame: pl.DataFrame) -> pl.DataFrame:
        try:
            cleaned = self._clean_common(frame, "FRANKLIN_SALES")
            if cleaned["quantity"].lt(0).any():
                raise IntegrityError("FRANKLIN_SALES daily quantities must be positive.")
            if cleaned.select("date", "category", "subcategory", "product_name").is_duplicated().any():
                raise IntegrityError("Duplicate daily product keys in FRANKLIN_SALES; review the export.")
        except pl.exceptions.PolarsError as exc:
            raise IntegrityError(f"Cannot validate Franklin Sales: {exc}") from exc
        else:
            return cleaned

    def clean_franklin_store(self, frame: pl.DataFrame) -> pl.DataFrame:
        try:
            cleaned = self._clean_common(frame, "FRANKLIN_STORE")
            movement = cleaned["movement"].map_elements(_normalized, return_dtype=pl.String)
            document = cleaned["document"].map_elements(_normalized, return_dtype=pl.String)
            sale, credit = movement.eq("venta"), movement.eq("nota de credito")
            if not (sale | credit).all():
                raise IntegrityError("Unrecognized FRANKLIN_STORE movement.")
            if ((sale & cleaned["quantity"].lt(0)) | (credit & cleaned["quantity"].gt(0))).any():
                raise IntegrityError("Movement and quantity signs disagree in FRANKLIN_STORE.")
            if not document.str.contains("nota de credito", literal=True).eq(credit).all():
                raise IntegrityError("Movement and document type disagree in FRANKLIN_STORE.")
        except pl.exceptions.PolarsError as exc:
            raise IntegrityError(f"Cannot validate Franklin Store: {exc}") from exc
        else:
            return cleaned

    def clean_mut_sales(self, frame: pl.DataFrame) -> pl.DataFrame:
        try:
            cleaned = self._clean_common(frame, "MUT_SALES")
            if cleaned["quantity"].lt(0).any():
                raise IntegrityError("MUT quantities must be positive; refunds need an explicit rule.")
            for column in ("time", "delivery_time"):
                if column in cleaned.columns:
                    populated = cleaned[column].drop_nulls().filter(cleaned[column].drop_nulls().ne(""))
                    if not populated.str.contains(r"^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$").all():
                        raise IntegrityError(f"Invalid HH:MM:SS values in {column}.")
            if "currency" in cleaned.columns and not cleaned["currency"].drop_nulls().eq("CLP").all():
                raise IntegrityError("Unexpected MUT currency; amount conversion is not configured.")
        except pl.exceptions.PolarsError as exc:
            raise IntegrityError(f"Cannot validate MUT: {exc}") from exc
        else:
            return cleaned

    def clean_mut_store(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Accept MUT_STORE as an alias for the repository's MUT_SALES layout."""
        return self.clean_mut_sales(frame)

    @staticmethod
    def _warning(code: str, count: int, **details: Any) -> dict[str, Any]:
        return {"code": code, "count": int(count), **details}

    def _audit(self, original: pl.DataFrame, cleaned: pl.DataFrame, source: str,
               controls: dict[str, Any], baseline: list[str] | None) -> dict[str, Any]:
        warnings = []
        data = cleaned.drop("source_row")
        duplicates = data.height - data.unique().height
        if duplicates:
            warnings.append(self._warning("repeated_lines_preserved", duplicates))
        whitespace = sum((data[c] != data[c].str.strip_chars()).sum()
                         for c, dtype in data.schema.items() if dtype == pl.String)
        if whitespace:
            warnings.append(self._warning("surrounding_whitespace_preserved", whitespace))
        unlabeled = [c for c in data.columns if c.startswith("source_unlabeled_")]
        if unlabeled:
            warnings.append(self._warning("unlabeled_columns_preserved", len(unlabeled), columns=unlabeled))
        future = (data["date"].dt.date() > datetime.now(ZoneInfo("America/Santiago")).date()).sum()
        if future:
            warnings.append(self._warning("future_dates", future))
        if source.startswith("MUT"):
            unpaid = data["status"].map_elements(_normalized, return_dtype=pl.String).ne("pagado").sum()
            if unpaid:
                warnings.append(self._warning("non_paid_lines_preserved", unpaid))
            for identifier in ("order_reference", "order_id"):
                conflicts = data.group_by(identifier).agg(pl.col("date").n_unique().alias("dates"),
                                                         pl.col("status").n_unique().alias("statuses"))
                count = conflicts.filter((pl.col("dates") > 1) | (pl.col("statuses") > 1)).height
                if count:
                    warnings.append(self._warning(f"inconsistent_{identifier}", count))
        names = sorted(data["product_name"].unique().to_list())
        if baseline is not None:
            new_names = sorted(set(names) - set(baseline))
            if new_names:
                normalized_baseline = {_normalized(n): n for n in baseline}
                candidates = {}
                for name in new_names:
                    matches = get_close_matches(_normalized(name), normalized_baseline, n=3, cutoff=0.85)
                    if matches:
                        candidates[name] = [normalized_baseline[m] for m in matches]
                warnings.append(self._warning("unrecognized_product_names", len(new_names),
                                              names=new_names, possible_matches=candidates))
        ranking = controls.get("ranking")
        if ranking is not None:
            ranking = self._drop_empty_rows(ranking)
            key = ["category", "subcategory", "product_name"]
            self._require_values(ranking, set(key) | {"quantity"})
            ranking = ranking.with_columns(self._parse_numbers(ranking["quantity"], integer=True))
            if ranking.select(key).is_duplicated().any():
                raise IntegrityError("Duplicate products in the Franklin ranking control.")
            detail_units = data.group_by(key).agg(pl.col("quantity").sum().alias("detail_units"))
            ranking_units = ranking.group_by(key).agg(pl.col("quantity").sum().alias("ranking_units"))
            comparison = detail_units.join(ranking_units, on=key, how="full", coalesce=True)
            if comparison.filter(pl.col("detail_units").fill_null(0) != pl.col("ranking_units").fill_null(0)).height:
                raise IntegrityError("Franklin detail quantities do not reconcile to the ranking.")
        elif source == "FRANKLIN_SALES":
            warnings.append(self._warning("ranking_control_unavailable", 1))
        summary = controls.get("summary", {})
        if source == "FRANKLIN_STORE":
            for label, actual in (
                ("filas de detalle", data.height),
                ("documentos de venta", data.filter(pl.col("quantity") > 0).select("document", "document_number").unique().height),
                ("documentos de nc", data.filter(pl.col("quantity") < 0).select("document", "document_number").unique().height),
            ):
                if label in summary and summary[label] != actual:
                    raise IntegrityError(f"Store summary does not reconcile: {label}.")
            if not summary:
                warnings.append(self._warning("store_summary_unavailable", 1))
        if controls.get("summary_errors"):
            warnings.append(self._warning("excel_errors_in_auxiliary_summary", controls["summary_errors"]))
        dates = data["date"]
        missing_days = (dates.max() - dates.min()).days + 1 - dates.n_unique()
        if missing_days:
            warnings.append(self._warning("days_without_observations", missing_days))
        if cleaned.height > original.height:
            raise IntegrityError("Cleaning unexpectedly added rows.")
        expected = cleaned.select("source_row").join(self._with_source_rows(original), on="source_row",
                                                     how="left", maintain_order="left", validate="1:1")
        if expected["product_name"].to_list() != data["product_name"].to_list():
            raise IntegrityError("Cleaning unexpectedly changed product names.")
        if not self._parse_numbers(expected["quantity"], integer=True).equals(data["quantity"]):
            raise IntegrityError("Cleaning unexpectedly changed quantities.")
        return {
            "input_rows": original.height, "output_rows": data.height,
            "empty_rows_removed": original.height - data.height,
            "quantity_total": sum(data["quantity"].to_list()),
            "date_min": dates.min().date().isoformat(), "date_max": dates.max().date().isoformat(),
            "null_counts": data.null_count().row(0, named=True),
            "product_names": names, "warnings": warnings,
        }

    def _valid_output(self, record: dict[str, Any]) -> bool:
        try:
            if record.get("status") != "processed" or record.get("pipeline_version") != PIPELINE_VERSION:
                return False
            if not isinstance(record.get("output"), str):
                return False
            path = (self.root / record["output"]).resolve()
            if not path.is_relative_to(self.del_dir.resolve()) or path.suffix != ".parquet":
                return False
            if not path.is_file() or _sha256(path) != record.get("output_sha256"):
                self.logger.warning("DEL artifact missing or modified; rebuilding %s", record["output"])
                return False
            return True
        except OSError as exc:
            raise ELTError(f"Cannot verify DEL artifact: {exc}") from exc

    def _write_parquet(self, frame: pl.DataFrame, output: Path) -> None:
        temporary = None
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
            frame.write_parquet(temporary, compression="snappy")
            assert_frame_equal(frame, pl.read_parquet(temporary), check_exact=True)
            os.replace(temporary, output)
        except Exception as exc:
            raise ELTError(f"Cannot publish verified Parquet {output}: {exc}") from exc
        finally:
            if temporary is not None:
                _remove_temporary(temporary, self.logger)

    def _retire_legacy_output(self, previous: dict[str, Any], output: str,
                              path: Path, log: dict[str, Any]) -> None:
        previous = previous if previous.get("status") == "processed" else previous.get("last_success") or {}
        old_output = previous.get("output")
        if not old_output or old_output == output:
            return
        try:
            old_path = (self.root / old_output).resolve()
            expected = self.del_dir / path.relative_to(self.raw_dir).with_suffix(path.suffix.lower() + ".parquet")
            if (old_path != expected.resolve() or not old_path.is_relative_to(self.del_dir.resolve())
                    or not old_path.is_file()
                    or any(entry.get("output") == old_output for entry in log["files"].values())):
                return
            if _sha256(old_path) != previous.get("output_sha256"):
                self.logger.warning("Legacy output was modified and will be retained: %s", old_output)
                return
            old_path.unlink()
        except OSError:
            self.logger.warning("Could not retire legacy output %s; execution continues", old_output, exc_info=True)
        else:
            self.logger.info("Legacy output retired: %s", old_output)

    def _save_state(self, log: dict[str, Any]) -> None:
        with self._stage("Processing state persistence"):
            _atomic_json(self.log_path, log, self.logger)

    def _failure_record(self, source: str, previous: dict[str, Any],
                        exc: Exception, severity: str) -> dict[str, Any]:
        return {
            "status": "failed", "severity": severity, "source": source,
            "failed_at": _utc_now(), "error": str(exc),
            "last_success": previous if previous.get("status") == "processed" else previous.get("last_success"),
        }

    def _process_file(self, source: str, path: Path, log: dict[str, Any]) -> FileResult:
        relative = path.relative_to(self.root).as_posix()
        previous = log["files"].get(relative, {})
        try:
            with self._stage(f"Source fingerprint [{relative}]", input_stage=True):
                digest = _sha256(path)
            with self._stage(f"Processing history checked [{relative}]"):
                matching = next((entry for entry in [previous, *log["files"].values()]
                                 if entry.get("source") == source and entry.get("source_sha256") == digest
                                 and self._valid_output(entry)), None)
            if matching is not None:
                log["files"][relative] = dict(matching)
                self._save_state(log)
                self._retire_legacy_output(previous, matching["output"], path, log)
                self.logger.info("Skipped verified export: %s", relative)
                return FileResult(relative, "skipped", matching["output"], matching["report"]["output_rows"],
                                  matching["report"]["warnings"])
            with self._stage(f"Input loaded [{relative}]", input_stage=True):
                frame, sheet, controls = self._load(path, source)
            with self._stage(f"Input cleaned [{relative}]", input_stage=True):
                cleaned = getattr(self, f"clean_{source.lower()}")(frame)
            with self._stage(f"Integrity checks [{relative}]", input_stage=True):
                report = self._audit(frame, cleaned, source, controls, log["product_catalog"].get(source))
                for other_path, entry in log["files"].items():
                    if (other_path != relative and entry.get("source") == source
                            and entry.get("status") == "processed"
                            and entry["report"]["date_min"] <= report["date_max"]
                            and entry["report"]["date_max"] >= report["date_min"]):
                        report["warnings"].append(self._warning("overlapping_export_period", 1, other_file=other_path))
            for warning in report["warnings"]:
                self.logger.warning("Data check [%s]: %s", relative, json.dumps(warning, ensure_ascii=False))
            with self._stage(f"Source stability verified [{relative}]", input_stage=True):
                if _sha256(path) != digest:
                    raise IntegrityError("RAW changed during processing; retry after it is saved.")
            with self._stage(f"Parquet published [{relative}]"):
                output = self._output_path(path)
                cleaned = cleaned.with_columns(pl.lit(source).alias("source"),
                                               pl.lit(relative).alias("source_file"),
                                               pl.lit(sheet).alias("source_sheet"),
                                               pl.lit(digest).alias("source_sha256"))
                self._write_parquet(cleaned, output)
                record = {
                    "status": "processed", "source": source, "source_sha256": digest,
                    "pipeline_version": PIPELINE_VERSION, "processed_at": _utc_now(),
                    "output": output.relative_to(self.root).as_posix(),
                    "output_sha256": _sha256(output), "report": report,
                }
            log["files"][relative] = record
            self._save_state(log)
        except IntegrityError as exc:
            self.logger.warning("Rejected export; batch continues [%s]: %s", relative, exc, exc_info=True)
            log["files"][relative] = self._failure_record(source, previous, exc, "WARNING")
            self._save_state(log)
            return FileResult(relative, "failed", error=str(exc))
        except Exception as exc:
            log["files"][relative] = self._failure_record(source, previous, exc, "ERROR")
            # Preserve the original fatal cause even if saving its record also fails.
            try:
                self._save_state(log)
            except Exception:
                self.logger.warning("Could not persist the fatal file record [%s]", relative, exc_info=True)
            raise ELTError(f"Stopped while processing {relative}: {exc}") from exc
        else:
            self._retire_legacy_output(previous, record["output"], path, log)
            self.logger.info("Processed export: %s; rows=%d; warnings=%d", relative, cleaned.height, len(report["warnings"]))
            return FileResult(relative, "processed", record["output"], cleaned.height, report["warnings"])

    def _run_batch(self) -> RunResult:
        with self._stage("RAW discovery"):
            files = self.discover_files()
        with self._stage("Output names validated"):
            self._check_output_collisions(files)
        with self._stage("Processing history loaded"):
            log = self._read_log()
        result = RunResult()
        for source, path in files:
            result.files.append(self._process_file(source, path, log))
        with self._stage("Product catalogs and run summary updated"):
            failed_sources = {Path(item.source_file).relative_to("Data/RAW").parts[0]
                              for item in result.files if item.status == "failed"}
            for source in SOURCES:
                if source not in log["product_catalog"] and source not in failed_sources:
                    names = {name for entry in log["files"].values()
                             if entry.get("source") == source and entry.get("status") == "processed"
                             for name in entry["report"]["product_names"]}
                    if names:
                        log["product_catalog"][source] = sorted(names)
            log["last_run_at"] = _utc_now()
            log["last_run"] = {"processed": result.processed, "skipped": result.skipped, "failed": result.failed}
            has_warnings = result.failed or any(item.warnings for item in result.files)
            log["last_run_status"] = "completed_with_warnings" if has_warnings else "completed"
        self._save_state(log)
        return result

    def run(self) -> RunResult:
        """Log each completed stage, continue on bad inputs, and stop on fatal errors."""
        original_logger = self.logger
        try:
            with event_logger(self.root, self.logger) as logger:
                self.logger = logger
                logger.info("Batch started; pipeline_version=%s", PIPELINE_VERSION)
                try:
                    with self._locked():
                        outcome = self._run_batch()
                except (Exception, KeyboardInterrupt) as exc:
                    logger.error("ELT stopped: %s", exc, exc_info=True)
                    if isinstance(exc, ELTError):
                        raise
                    raise ELTError(f"ELT stopped: {exc}") from exc
                else:
                    logger.info("Batch completed: %d processed, %d skipped, %d rejected",
                                outcome.processed, outcome.skipped, outcome.failed)
                    return outcome
        finally:
            self.logger = original_logger


def main(repository_root: str | Path | None = None) -> int:
    """Own the standalone logger and convert fatal exceptions to exit code 1."""
    try:
        root = (Path(repository_root) if repository_root is not None else Path(__file__).resolve().parents[1]).resolve()
        with event_logger(root, console=True) as logger:
            try:
                ELT(root, logger=logger).run()
            except ELTError:
                return 1  # run() has already recorded the fatal error and traceback.
            except (Exception, KeyboardInterrupt):
                logger.error("Unexpected failure in main", exc_info=True)
                return 1
            else:
                return 0
    except (Exception, KeyboardInterrupt):
        # If the event file itself is unavailable, stderr remains the fallback.
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        LOGGER.error("ELT could not initialize its logger", exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
