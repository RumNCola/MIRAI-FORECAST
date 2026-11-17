"""Standard-library runtime support shared by setup and the application entry point."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class PipelineError(RuntimeError):
    """A fatal failure that must stop setup or the analysis pipeline."""


class _StrictFileHandler(logging.FileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        raise PipelineError("Cannot write pipeline.log") from sys.exception()


@contextmanager
def pipeline_logger(root: Path, logger: logging.Logger | None = None) -> Iterator[logging.Logger]:
    logger = logger or logging.getLogger(f"mirai.pipeline.{hashlib.sha256(str(root).encode()).hexdigest()[:12]}")
    handlers: list[logging.Handler] = []
    previous_level, previous_propagate = logger.level, logger.propagate
    try:
        logger.setLevel(logging.INFO)
        logger.propagate = False
        formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)
        handlers.append(console)
        file_handler = _StrictFileHandler(root / "pipeline.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        handlers.append(file_handler)
        yield logger
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


@contextmanager
def pipeline_lock(root: Path, logger: logging.Logger) -> Iterator[None]:
    path = root / ".pipeline.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise PipelineError("Another setup or pipeline run holds .pipeline.lock. "
                            "Remove it only after confirming that the previous process stopped.") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"pid={os.getpid()}\n")
        logger.info("Pipeline execution lock acquired")
        yield
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not release .pipeline.lock", exc_info=True)


def run_logged(command: list[str], *, label: str, root: Path,
               logger: logging.Logger, env: dict[str, str] | None = None) -> None:
    """Stream child output without a shell; report success only after exit code 0."""
    process = None
    warning_block = False
    try:
        process = subprocess.Popen(
            command, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert process.stdout is not None
        with process.stdout:
            for line in process.stdout:
                message = line.rstrip()
                if message:
                    warning = bool(re.search(r"\bwarning(?:s)?\b|^Aviso:", message, re.IGNORECASE))
                    numbered_warning = warning_block and bool(re.match(r"^\s*\d+:", message))
                    warning_block = warning or numbered_warning
                    warning = warning or numbered_warning
                    logger.log(logging.WARNING if warning else logging.INFO, "[%s] %s", label, message)
        if process.wait() != 0:
            raise PipelineError(f"{label} failed with exit code {process.returncode}; see pipeline.log")
    except (Exception, KeyboardInterrupt):
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    else:
        logger.info("%s completed", label)


def _r_candidates(root: Path) -> list[Path]:
    candidates = []
    for directory in sorted((root / ".runtime").glob("R-*"), reverse=True):
        candidates.extend((directory / "bin" / "Rscript.exe", directory / "bin" / "x64" / "Rscript.exe"))
    executable = shutil.which("Rscript")
    if executable:
        candidates.append(Path(executable))
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\R-core\R") as key:
                    directory = Path(winreg.QueryValueEx(key, "InstallPath")[0])
                    candidates.extend((directory / "bin" / "Rscript.exe", directory / "bin" / "x64" / "Rscript.exe"))
            except OSError:
                continue
        for directory in (Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "R",
                          Path(os.environ.get("LOCALAPPDATA", str(root))) / "Programs" / "R"):
            for install in sorted(directory.glob("R-*"), reverse=True):
                candidates.extend((install / "bin" / "Rscript.exe", install / "bin" / "x64" / "Rscript.exe"))
    return candidates


def find_rscript(root: Path) -> Path | None:
    explicit = os.environ.get("MIRAI_RSCRIPT")
    if explicit:
        path = Path(explicit) if Path(explicit).is_file() else Path(shutil.which(explicit) or explicit)
        if not path.is_file():
            raise PipelineError("MIRAI_RSCRIPT does not point to an existing Rscript executable")
        return path.resolve()
    return next((path.resolve() for path in _r_candidates(root) if path.is_file()), None)


def r_environment(root: Path, rscript: Path) -> dict[str, str]:
    """Keep packages separated by R major/minor version and architecture."""
    environment = dict(os.environ)
    if os.name == "nt":
        # POSIX locale strings inherited from terminals may be invalid in Windows R.
        for key in list(environment):
            if key == "LANG" or key.startswith("LC_"):
                environment.pop(key)
        environment["LANGUAGE"] = "en"
    probe = subprocess.run(
        [str(rscript), "--vanilla", "-e",
         "cat(paste(R.version$major, strsplit(R.version$minor, '.', fixed=TRUE)[[1]][1], R.version$arch, sep='-'))"],
        cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    tag = probe.stdout.strip()
    if probe.returncode or not re.fullmatch(r"\d+-\d+-[A-Za-z0-9_]+", tag):
        raise PipelineError(f"Cannot determine the R runtime version: {probe.stderr.strip()}")
    return {
        **environment, "MIRAI_PYTHON": sys.executable, "MIRAI_RSCRIPT": str(rscript),
        "PYTHONUTF8": "1", "R_LIBS_USER": (root / ".runtime" / "r-library" / tag).as_posix(),
    }
