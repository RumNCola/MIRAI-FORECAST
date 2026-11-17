"""Run incremental ELT, then the Franklin and MUT demand models."""

from __future__ import annotations

import logging
import json
import sys
from pathlib import Path

from settings.config import CRAN_MIRROR, MODEL_SCRIPTS, REPOSITORY_ROOT, ModelSettings, get_model_config
from src.runtime import PipelineError, find_rscript, pipeline_lock, pipeline_logger, r_environment, run_logged


def run_pipeline(root: Path, logger: logging.Logger, *,
                 model_settings: dict[str, ModelSettings] | None = None,
                 data_root: Path | None = None, strict_inputs: bool = False) -> None:
    """Validate runtime/configuration before starting the ordered application flow."""
    if model_settings is not None and not model_settings:
        raise ValueError("Select at least one site")
    sites = tuple(model_settings) if model_settings is not None else tuple(MODEL_SCRIPTS)
    workspace = data_root or root
    configurations = {site: get_model_config(site, repository_root=workspace,
                      settings=model_settings[site] if model_settings is not None else None) for site in sites}
    for site in sites:
        relative = MODEL_SCRIPTS[site]
        if not (root / relative).is_file():
            raise PipelineError(f"Missing {site} model script: {relative}")
    rscript = find_rscript(root)
    if rscript is None:
        raise PipelineError("Rscript is missing. Run main.bat to prepare Python, R and their packages.")
    environment = r_environment(root, rscript)
    packages = sorted({package for config in configurations.values() for package in config["required_r_packages"]})
    run_logged([str(rscript), "--vanilla", str(root / "scripts" / "prepare_r.R"),
                "check", environment["R_LIBS_USER"], CRAN_MIRROR, *packages],
               label="R dependency check", root=root, logger=logger, env=environment)
    logger.info("Configuration and runtime validation completed")

    # Lazy import lets main record missing/broken Python dependencies in the log.
    from src.elt import ELT
    outcome = ELT(workspace, logger=logger).run()
    if outcome.failed:
        logger.warning("ELT rejected %d input files; models will consume accepted DEL artifacts only", outcome.failed)
    logger.info("ELT completed: %d processed, %d skipped, %d rejected",
                outcome.processed, outcome.skipped, outcome.failed)
    if strict_inputs and outcome.failed:
        raise PipelineError("Hay archivos rechazados. Revisa el detalle, corrige los Excel y vuelve a ejecutar.")
    for site in sites:
        relative = MODEL_SCRIPTS[site]
        model_environment = dict(environment)
        # Never mutate process-wide environment: browser sessions may coexist.
        model_environment.pop("MIRAI_MODEL_CONFIG", None)
        if model_settings is not None or data_root is not None:
            config_path = workspace / ".runtime" / f"{site.lower()}-config.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(configurations[site], ensure_ascii=True), encoding="utf-8")
            model_environment["MIRAI_MODEL_CONFIG"] = str(config_path.resolve())
        run_logged([str(rscript), "--vanilla", str(root / relative)],
                   label=f"{site} demand model", root=root, logger=logger, env=model_environment)
        logger.info("%s analysis saved in %s", site, configurations[site]["output_directory"])


def main(repository_root: str | Path | None = None) -> int:
    try:
        root = Path(repository_root).resolve() if repository_root is not None else REPOSITORY_ROOT
        with pipeline_logger(root) as logger:
            try:
                with pipeline_lock(root, logger):
                    logger.info("Analysis pipeline started")
                    run_pipeline(root, logger)
            except (Exception, KeyboardInterrupt) as exc:
                logger.error("Analysis pipeline stopped: %s", exc, exc_info=True)
                return 1
            else:
                logger.info("Analysis pipeline completed successfully")
                return 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f"ERROR: Cannot initialize pipeline logging: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
