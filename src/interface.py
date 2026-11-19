"""Local, isolated analysis runs used by the Streamlit interface."""
from __future__ import annotations

import hashlib
import io
import logging
import math
import re
import zipfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from main import run_pipeline
from settings.config import (INVENTORY_PARAMETER_SHEETS, INVENTORY_PARAMETERS_WORKBOOK, MODEL_SETTINGS, REPOSITORY_ROOT,
                             InventorySettings, InventorySiteSettings, ModelSettings, get_model_config)
from src.runtime import pipeline_lock, pipeline_logger

PARAMETERS_IN_RUN = Path("Data") / "PARAM" / "inventario_parametros.xlsx"


def default_parameters_workbook(root: Path | None = None) -> bytes:
    """Bytes of the base inventory parameters (shelf life and dish composition)."""
    return ((root or REPOSITORY_ROOT) / INVENTORY_PARAMETERS_WORKBOOK).read_bytes()


def _cell(value) -> str | float | None:
    """Comparable form of a cell: trimmed text, numbers as float, blanks as None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else str(value)
    text = str(value).strip()
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return text
    # "NaN", "inf"... are text for the validators, which only accept finite numbers.
    return number if math.isfinite(number) else text


def read_parameters_workbook(content: bytes) -> dict[str, list[tuple]]:
    """Validate the inventory workbook and return its rows per sheet, in the declared column order.

    Raises ValueError with a message for the user when the format is not respected: missing sheets
    or columns, unnamed or repeated ingredients, non-positive values, unknown sites or ingredients.
    """
    from openpyxl import load_workbook
    if not content or not zipfile.is_zipfile(io.BytesIO(content)):
        raise ValueError("El archivo de parámetros no es un Excel válido (.xlsx).")
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError(f"No se pudo abrir el Excel de parámetros: {exc}") from exc
    try:
        rows = {}
        for sheet, columns in INVENTORY_PARAMETER_SHEETS.items():
            if sheet not in workbook.sheetnames:
                raise ValueError(f"Al Excel de parámetros le falta la hoja «{sheet}».")
            values = [row for row in workbook[sheet].iter_rows(values_only=True)
                      if any(_cell(cell) is not None for cell in row)]
            header = [str(cell).strip() if cell is not None else "" for cell in (values[0] if values else ())]
            missing = [column for column in columns if column not in header]
            if missing:
                raise ValueError(f"La hoja «{sheet}» necesita las columnas: {', '.join(missing)}.")
            repeated = sorted({name for name in header if name and header.count(name) > 1})
            if repeated:
                raise ValueError(f"La hoja «{sheet}» repite las columnas: {', '.join(repeated)}.")
            positions = [header.index(column) for column in columns]
            rows[sheet] = [tuple(_cell(row[i]) if i < len(row) else None for i in positions) for row in values[1:]]
    finally:
        workbook.close()
    shelf = set()
    for number, (name, days, *_rest) in enumerate(rows["vida_util"], 2):
        name = str(name or "").strip().lower()
        if not name:
            raise ValueError(f"vida_util, fila {number}: falta el ingrediente.")
        if name in shelf:
            raise ValueError(f"vida_util, fila {number}: el ingrediente «{name}» está repetido.")
        if days is not None and (not isinstance(days, float) or days <= 0):
            raise ValueError(f"vida_util, fila {number}: «{name}» debe tener una vida útil numérica positiva o vacía.")
        shelf.add(name)
    if not rows["composicion"]:
        raise ValueError("composicion: agrega al menos un plato.")
    seen = set()
    for number, (site, product, ingredient, quantity, _source) in enumerate(rows["composicion"], 2):
        site = str(site or "").strip().upper()
        ingredient = str(ingredient or "").strip().lower()
        if site not in {*MODEL_SETTINGS, "AMBOS"}:
            raise ValueError(f"composicion, fila {number}: local debe ser {', '.join(MODEL_SETTINGS)} o AMBOS.")
        if product is None or not ingredient:
            raise ValueError(f"composicion, fila {number}: faltan producto o ingrediente.")
        if ingredient not in shelf:
            raise ValueError(f"composicion, fila {number}: «{ingredient}» no está en la hoja vida_util.")
        if not isinstance(quantity, float) or quantity <= 0:
            raise ValueError(f"composicion, fila {number}: cantidad_por_plato debe ser un número positivo.")
        key = (site, str(product).strip().lower(), ingredient)
        if key in seen:
            raise ValueError(f"composicion, fila {number}: «{product}» ya tiene «{ingredient}» en {site}.")
        seen.add(key)
    return rows


def validate_parameters_workbook(content: bytes) -> None:
    """Check the editable inventory workbook before a run or before replacing the base."""
    read_parameters_workbook(content)


def compare_parameters_workbooks(current: bytes, new: bytes) -> list[dict]:
    """Rows added and removed per sheet. An edited row counts as one removed plus one added."""
    from collections import Counter
    old_rows, new_rows = read_parameters_workbook(current), read_parameters_workbook(new)
    summary = []
    for sheet in INVENTORY_PARAMETER_SHEETS:
        before, after = Counter(old_rows[sheet]), Counter(new_rows[sheet])
        summary.append({"hoja": sheet, "filas_actuales": sum(before.values()), "filas_nuevas": sum(after.values()),
                        "añadidas": sum((after - before).values()), "eliminadas": sum((before - after).values()),
                        "sin_cambios": sum((before & after).values())})
    return summary


def replace_default_parameters(content: bytes, root: Path | None = None) -> Path:
    """Validate and install a new base workbook; the previous one is kept in out/parametros_respaldo/."""
    validate_parameters_workbook(content)
    root = root or REPOSITORY_ROOT
    target = root / INVENTORY_PARAMETERS_WORKBOOK
    backup = root / "out" / "parametros_respaldo" / f"inventario_parametros-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}.xlsx"
    if target.is_file():
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(target.read_bytes())
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(content)
    temporary.replace(target)
    return backup


def validate_uploads(uploads: dict[str, list[tuple[str, bytes]]],
                     settings: dict[str, ModelSettings]) -> None:
    if not settings or set(uploads) != set(settings):
        raise ValueError("Selecciona los locales y carga sus archivos.")
    for site, configuration in settings.items():
        get_model_config(site, settings=configuration)
        if not uploads[site]:
            raise ValueError(f"Falta cargar al menos un Excel de {site}.")
        seen = set()
        for name, content in uploads[site]:
            if Path(name).suffix.lower() not in {".xlsx", ".xlsm"}:
                raise ValueError(f"{name}: utiliza Excel .xlsx o .xlsm.")
            if not content or not zipfile.is_zipfile(io.BytesIO(content)):
                raise ValueError(f"{name}: el archivo no es un Excel válido.")
            digest = hashlib.sha256(content).hexdigest()
            if digest in seen:
                raise ValueError(f"{site}: cargaste dos veces el mismo archivo ({name}).")
            seen.add(digest)


def create_run(root: Path, uploads: dict[str, list[tuple[str, bytes]]],
               settings: dict[str, ModelSettings], parameters: bytes | None = None) -> Path:
    """Store the uploads and the inventory parameters used by this run (uploaded or repository default)."""
    validate_uploads(uploads, settings)
    if parameters is not None:
        validate_parameters_workbook(parameters)
    run = root / "out" / "ejecuciones" / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    run.mkdir(parents=True)
    for site, files in uploads.items():
        for index, (name, content) in enumerate(files, 1):
            # Unique subdirectories avoid stem collisions and untrusted upload paths.
            safe_name = re.sub(r"[^\w. -]", "_", name.replace("\\", "/").split("/")[-1])
            destination = run / "Data" / "RAW" / settings[site].source / f"{index:03d}" / safe_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    default = REPOSITORY_ROOT / INVENTORY_PARAMETERS_WORKBOOK
    if parameters is not None or default.is_file():
        (run / PARAMETERS_IN_RUN).parent.mkdir(parents=True, exist_ok=True)
        (run / PARAMETERS_IN_RUN).write_bytes(parameters if parameters is not None else default.read_bytes())
    return run


def execute_run(root: Path, run: Path, settings: dict[str, ModelSettings],
                handler: logging.Handler | None = None, *,
                inventory: InventorySettings | None = None,
                inventory_sites: dict[str, InventorySiteSettings] | None = None,
                report_start_today: bool = False) -> None:
    with pipeline_logger(run) as logger:
        if handler:
            logger.addHandler(handler)
        try:
            with pipeline_lock(root, logger):
                logger.info("Iniciando análisis de %s", ", ".join(settings))
                parameters = run / PARAMETERS_IN_RUN
                run_pipeline(root, logger, model_settings=settings, data_root=run, strict_inputs=True,
                             inventory_settings=inventory, inventory_sites=inventory_sites,
                             parameters_workbook=parameters if parameters.is_file() else None,
                             report_start_today=report_start_today)
                logger.info("Análisis finalizado correctamente")
        except Exception:
            logger.exception("No se pudo completar el análisis")
            raise
        finally:
            if handler:
                logger.removeHandler(handler)


def result_zip(run: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((run / "out").rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(run / "out"))
    return buffer.getvalue()
