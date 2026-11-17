"""Shared demand-model settings and the JSON contract consumed by R.

Edit the constants and MODEL_SETTINGS here. Importing this module never starts
ELT or model execution. A future UI can construct/replace ModelSettings objects.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIN_PYTHON = (3, 12)
HORIZON_DAYS = 28
R_PACKAGES = ("arrow", "digest", "jsonlite", "tzdb", "writexl")
R_VERSION = "4.6.2"
CRAN_MIRROR = "https://cloud.r-project.org"
MODEL_SCRIPTS = {
    "FRANKLIN": "scripts/modelo_demanda_Franklin.R",
    "MUT": "scripts/modelo_demanda_MUT.R",
}


@dataclass(frozen=True)
class ModelSettings:
    source: str
    model: str
    operating_weekdays: tuple[int, ...]
    output_subdirectory: str
    horizon_days: int = HORIZON_DAYS
    date_column: str = "date"
    product_column: str = "product_name"
    quantity_column: str = "quantity"
    paid_statuses: tuple[str, ...] = ()
    reject_overlapping_dates: bool = True
    export_excel: bool = True
    export_parquet: bool = True
    generate_plots: bool = True
    residual_min_observations: int = 5
    history_plot_days: int = 45
    plot_width: int = 1800
    plot_height: int = 1350
    plot_resolution: int = 150

    def validate(self) -> None:
        if self.model not in {"dia", "dia_mes_anual"}:
            raise ValueError("model must be 'dia' or 'dia_mes_anual'")
        for name in ("horizon_days", "residual_min_observations", "history_plot_days",
                     "plot_width", "plot_height", "plot_resolution"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.residual_min_observations < 2:
            raise ValueError("residual_min_observations must be at least 2")
        weekdays = self.operating_weekdays
        if (not weekdays or len(set(weekdays)) != len(weekdays)
                or any(type(day) is not int or not 1 <= day <= 7 for day in weekdays)):
            raise ValueError("operating_weekdays must contain unique ISO weekdays, Monday=1 to Sunday=7")
        for name in ("reject_overlapping_dates", "export_excel", "export_parquet", "generate_plots"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if not self.export_excel and not self.export_parquet:
            raise ValueError("Enable at least one table output format")
        for column in (self.date_column, self.product_column, self.quantity_column):
            if not isinstance(column, str) or not column.strip():
                raise ValueError("Model input column names must be nonempty strings")
        if len({self.date_column, self.product_column, self.quantity_column}) != 3:
            raise ValueError("Date, product and quantity columns must be distinct")
        if any(not isinstance(status, str) or not status.strip() for status in self.paid_statuses):
            raise ValueError("paid_statuses must contain nonempty strings")


MODEL_SETTINGS = {
    "FRANKLIN": ModelSettings(
        source="FRANKLIN_SALES", model="dia_mes_anual",
        operating_weekdays=(5, 6, 7), output_subdirectory="franklin",
    ),
    "MUT": ModelSettings(
        source="MUT_SALES", model="dia", operating_weekdays=(1, 2, 3, 4, 5, 6, 7),
        output_subdirectory="mut", paid_statuses=("pagado",),
    ),
}


def get_model_config(site: str, *, settings: ModelSettings | None = None,
                     repository_root: str | Path | None = None) -> dict:
    """Return a validated, JSON-compatible configuration for Python or R callers."""
    site = site.upper()
    if site not in MODEL_SETTINGS:
        raise ValueError(f"Unknown site: {site}")
    settings = settings if settings is not None else MODEL_SETTINGS[site]
    settings.validate()
    allowed_sources = {"FRANKLIN": {"FRANKLIN_SALES"}, "MUT": {"MUT_SALES", "MUT_STORE"}}
    if settings.source not in allowed_sources[site]:
        raise ValueError(f"Unsupported source for {site}: {settings.source}")
    root = Path(repository_root).resolve() if repository_root is not None else REPOSITORY_ROOT
    subdirectory = Path(settings.output_subdirectory)
    if not settings.output_subdirectory or subdirectory.is_absolute() or ".." in subdirectory.parts:
        raise ValueError("output_subdirectory must be a relative directory under out")
    output_root = (root / "out").resolve()
    output = (output_root / subdirectory).resolve()
    if output == output_root or not output.is_relative_to(output_root):
        raise ValueError("Model output must stay inside a subdirectory of out")
    packages = R_PACKAGES if settings.export_excel else tuple(p for p in R_PACKAGES if p != "writexl")
    return {
        **asdict(settings), "schema_version": 1, "site": site,
        "repository_root": root.as_posix(),
        "input_directory": (root / "Data" / "DEL" / settings.source).as_posix(),
        "processing_log": (root / "elt_log.json").as_posix(),
        "output_directory": output.as_posix(),
        "graphs_directory": (output / "graphs").as_posix(),
        "workbook_filename": f"modelo_demanda_{'Franklin' if site == 'FRANKLIN' else 'MUT'}.xlsx",
        "required_r_packages": packages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export demand-model settings as JSON for R")
    parser.add_argument("--site", required=True, choices=tuple(MODEL_SETTINGS))
    args = parser.parse_args()
    try:
        print(json.dumps(get_model_config(args.site), ensure_ascii=True, allow_nan=False))
    except (TypeError, ValueError, OSError) as exc:
        print(f"Invalid model configuration: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
