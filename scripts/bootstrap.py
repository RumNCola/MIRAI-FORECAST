"""Prepare project dependencies using the standard library; never run an analysis."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from settings.config import CRAN_MIRROR, MIN_PYTHON, MODEL_SCRIPTS, R_VERSION, get_model_config
from src.runtime import PipelineError, find_rscript, pipeline_lock, pipeline_logger, r_environment, run_logged


def prepare_python(root: Path, logger: logging.Logger) -> None:
    requirements = root / "requirements.txt"
    state_path = root / ".runtime" / "python-setup.json"
    fingerprint = {
        "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
        "python": sys.version, "executable": sys.executable,
    }
    previous = None
    if state_path.exists():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            logger.warning("Python setup state is unreadable; dependencies will be checked again", exc_info=True)
    check = subprocess.run([sys.executable, "-m", "pip", "check"], cwd=root,
                           capture_output=True, text=True, check=False)
    imports = subprocess.run([sys.executable, "-c", "import polars, openpyxl"], cwd=root,
                             capture_output=True, text=True, check=False)
    if previous != fingerprint or check.returncode or imports.returncode:
        run_logged([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements)],
                   label="Python dependency installation", root=root, logger=logger)
        run_logged([sys.executable, "-m", "pip", "check"], label="Python dependency verification", root=root, logger=logger)
        run_logged([sys.executable, "-c", "import polars, openpyxl"],
                   label="Python import verification", root=root, logger=logger)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(fingerprint, indent=2), encoding="utf-8")
        os.replace(temporary, state_path)
    else:
        logger.info("Python dependencies are already prepared")


def install_r(root: Path, logger: logging.Logger) -> Path:
    if os.name != "nt":
        raise PipelineError("Automatic R installation is supported on Windows. Install R and run setup again.")
    if not re.fullmatch(r"\d+\.\d+\.\d+", R_VERSION):
        raise PipelineError("R_VERSION must have the form major.minor.patch")
    downloads = root / ".runtime" / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    installer = downloads / f"R-{R_VERSION}-win.exe"
    base_url = "https://cran.r-project.org/bin/windows/base"
    with urllib.request.urlopen(f"{base_url}/md5sum.R-{R_VERSION}.txt", timeout=60) as response:
        checksums = response.read().decode("utf-8")
    match = re.search(rf"\b([a-fA-F0-9]{{32}})\b[^\r\n]*R-{re.escape(R_VERSION)}-win\.exe", checksums)
    if not match:
        raise PipelineError("CRAN did not provide the expected R installer checksum")
    expected = match.group(1).lower()
    if not installer.is_file() or hashlib.md5(installer.read_bytes()).hexdigest() != expected:
        temporary = installer.with_suffix(".download")
        logger.info("Downloading the official R %s installer from CRAN", R_VERSION)
        try:
            with urllib.request.urlopen(f"{base_url}/{installer.name}", timeout=60) as response, temporary.open("wb") as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
            if hashlib.md5(temporary.read_bytes()).hexdigest() != expected:
                raise PipelineError("Downloaded R installer does not match CRAN's checksum")
            os.replace(temporary, installer)
        finally:
            temporary.unlink(missing_ok=True)
        logger.info("R installer downloaded and verified")
    destination = root / ".runtime" / f"R-{R_VERSION}"
    run_logged([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                "/NOICONS", "/TASKS=", "/COMPONENTS=main,x64,translations", f"/DIR={destination}"],
               label="Project R runtime installation", root=root, logger=logger)
    rscript = destination / "bin" / "Rscript.exe"
    if not rscript.is_file():
        rscript = destination / "bin" / "x64" / "Rscript.exe"
    if not rscript.is_file():
        raise PipelineError("R installer finished without creating Rscript.exe")
    return rscript.resolve()


def prepare_environment(root: Path, logger: logging.Logger) -> None:
    if sys.version_info < MIN_PYTHON:
        raise PipelineError(f"Python {'.'.join(map(str, MIN_PYTHON))} or newer is required")
    if sys.prefix == sys.base_prefix:
        raise PipelineError("Run setup inside the project virtual environment through main.bat")
    configurations = [get_model_config(site, repository_root=root) for site in MODEL_SCRIPTS]
    prepare_python(root, logger)
    rscript = find_rscript(root)
    if rscript is None:
        rscript = install_r(root, logger)
    logger.info("R runtime selected: %s", rscript)
    environment = r_environment(root, rscript)
    packages = sorted({package for config in configurations for package in config["required_r_packages"]})
    run_logged([str(rscript), "--vanilla", str(root / "scripts" / "prepare_r.R"),
                "install", environment["R_LIBS_USER"], CRAN_MIRROR, *packages],
               label="R dependency preparation", root=root, logger=logger, env=environment)


def main() -> int:
    try:
        with pipeline_logger(ROOT) as logger:
            try:
                with pipeline_lock(ROOT, logger):
                    logger.info("Environment preparation started")
                    prepare_environment(ROOT, logger)
            except (Exception, KeyboardInterrupt) as exc:
                logger.error("Environment preparation stopped: %s", exc, exc_info=True)
                return 1
            else:
                logger.info("Environment preparation completed")
                return 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f"ERROR: Cannot initialize setup logging: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
