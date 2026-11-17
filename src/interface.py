"""Local, isolated analysis runs used by the Streamlit interface."""
from __future__ import annotations

import hashlib
import io
import logging
import re
import zipfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from main import run_pipeline
from settings.config import ModelSettings, get_model_config
from src.runtime import pipeline_lock, pipeline_logger


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
               settings: dict[str, ModelSettings]) -> Path:
    validate_uploads(uploads, settings)
    run = root / "out" / "ejecuciones" / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    run.mkdir(parents=True)
    for site, files in uploads.items():
        for index, (name, content) in enumerate(files, 1):
            # Unique subdirectories avoid stem collisions and untrusted upload paths.
            safe_name = re.sub(r"[^\w. -]", "_", name.replace("\\", "/").split("/")[-1])
            destination = run / "Data" / "RAW" / settings[site].source / f"{index:03d}" / safe_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    return run


def execute_run(root: Path, run: Path, settings: dict[str, ModelSettings],
                handler: logging.Handler | None = None) -> None:
    with pipeline_logger(run) as logger:
        if handler:
            logger.addHandler(handler)
        try:
            with pipeline_lock(root, logger):
                logger.info("Iniciando análisis de %s", ", ".join(settings))
                run_pipeline(root, logger, model_settings=settings, data_root=run, strict_inputs=True)
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
