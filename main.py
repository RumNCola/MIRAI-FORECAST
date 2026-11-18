"""Run incremental ELT, the Franklin and MUT demand models, the inventory model and the production report.

Usage: main.bat [--comenzar-hoy]   (the report starts today instead of next Friday)
"""

from __future__ import annotations

import logging
import json
import sys
from datetime import date
from pathlib import Path

from settings.config import (CRAN_MIRROR, INVENTORY_PARAMETERS_WORKBOOK, INVENTORY_SCRIPT, INVENTORY_SETTINGS,
                             MODEL_SCRIPTS, REPORT_GROUPS_WORKBOOK, REPOSITORY_ROOT, InventorySettings,
                             InventorySiteSettings, ModelSettings,
                             get_inventory_config, get_model_config, load_user_defaults, user_defaults_path)
from src.runtime import PipelineError, find_rscript, pipeline_lock, pipeline_logger, r_environment, run_logged


def run_pipeline(root: Path, logger: logging.Logger, *,
                 model_settings: dict[str, ModelSettings] | None = None,
                 data_root: Path | None = None, strict_inputs: bool = False,
                 inventory_settings: InventorySettings | None = None,
                 inventory_sites: dict[str, InventorySiteSettings] | None = None,
                 parameters_workbook: Path | None = None,
                 report_start_today: bool = False, report_today: date | None = None) -> Path | None:
    """Validate runtime/configuration before starting the ordered application flow.

    Returns the production report (PDF) when it could be generated, otherwise None; the reason is
    logged and saved in out/reporte/estado.json.
    """
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
    inventory = None
    if inventory_settings is None or inventory_settings.enabled:
        workbook = parameters_workbook or root / INVENTORY_PARAMETERS_WORKBOOK
        inventory = get_inventory_config(model_settings, site_settings=inventory_sites, settings=inventory_settings,
                                         repository_root=workspace, parameters_workbook=workbook)
        if not (root / INVENTORY_SCRIPT).is_file():
            raise PipelineError(f"Missing inventory model script: {INVENTORY_SCRIPT}")
        if not workbook.is_file():
            raise PipelineError(f"Missing inventory parameters workbook: {workbook}")
    rscript = find_rscript(root)
    if rscript is None:
        raise PipelineError("Rscript is missing. Run main.bat to prepare Python, R and their packages.")
    environment = r_environment(root, rscript)
    packages = sorted({package for config in configurations.values() for package in config["required_r_packages"]}
                      | set(inventory["required_r_packages"] if inventory else ()))
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
    # Production report for the central kitchen: needs MUT and Franklin from this same run. It only
    # uses the demand outputs, so it runs before the inventory: a broken parameters workbook must
    # not leave the kitchen without its PDF.
    from src.report import generate_report
    report = generate_report(
        workspace, {site: (Path(configurations[site]["output_directory"]), configurations[site]["workbook_filename"])
                    for site in sites},
        root / REPORT_GROUPS_WORKBOOK, logger,
        service_level=(inventory_settings or INVENTORY_SETTINGS).default_service_level,
        today=report_today, start_today=report_start_today)
    if inventory is not None:
        config_path = workspace / ".runtime" / "inventory-config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(inventory, ensure_ascii=True), encoding="utf-8")
        inventory_environment = dict(environment)
        inventory_environment.pop("MIRAI_MODEL_CONFIG", None)
        inventory_environment["MIRAI_INVENTORY_CONFIG"] = str(config_path.resolve())
        run_logged([str(rscript), "--vanilla", str(root / INVENTORY_SCRIPT)],
                   label="Inventory model", root=root, logger=logger, env=inventory_environment)
        logger.info("Inventory analysis saved in %s", inventory["output_directory"])
    return report


def main(repository_root: str | Path | None = None, *, report_start_today: bool = False) -> int:
    try:
        root = Path(repository_root).resolve() if repository_root is not None else REPOSITORY_ROOT
        with pipeline_logger(root) as logger:
            try:
                with pipeline_lock(root, logger):
                    logger.info("Analysis pipeline started")
                    if user_defaults_path(root).is_file():
                        # Defaults saved from the interface's "Valores por defecto" view.
                        defaults = load_user_defaults(root)
                        logger.info("Using saved defaults from %s", user_defaults_path(root).name)
                        run_pipeline(root, logger, model_settings=defaults.model_settings,
                                     inventory_settings=defaults.inventory, inventory_sites=defaults.inventory_sites,
                                     report_start_today=report_start_today)
                    else:
                        run_pipeline(root, logger, report_start_today=report_start_today)
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
    unknown = [argument for argument in sys.argv[1:] if argument != "--comenzar-hoy"]
    if unknown:
        print(f"ERROR: Unknown option {unknown[0]}. Usage: main.bat [--comenzar-hoy]", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(report_start_today="--comenzar-hoy" in sys.argv[1:]))
