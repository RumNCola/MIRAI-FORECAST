"""Shared demand-model settings and the JSON contract consumed by R.

Edit the constants and MODEL_SETTINGS here. Importing this module never starts
ELT or model execution. A future UI can construct/replace ModelSettings objects.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, replace
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
INVENTORY_SCRIPT = "scripts/modelo_inventario.R"
INVENTORY_R_PACKAGES = ("jsonlite", "readxl", "writexl")
# Tabular, user-editable inputs of the inventory model: sheet "vida_util" (shelf life counted
# from the date a preparation is finished) and sheet "composicion" (ingredients per dish).
INVENTORY_PARAMETERS_WORKBOOK = "settings/inventario_parametros.xlsx"
INVENTORY_PARAMETER_SHEETS = {
    "vida_util": ("ingrediente", "vida_util_dias", "fuente"),
    "composicion": ("local", "producto", "ingrediente", "cantidad_por_plato", "fuente"),
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


@dataclass(frozen=True)
class InventorySiteSettings:
    """Replenishment calendar and shortage cost of one site.

    delivery_weekdays: ISO weekdays (Monday=1) on which an order can be dispatched to the site and
    becomes available there; each delivery covers the opening days until the next one. Preparations
    are finished on the dispatch day, and shelf life is counted from that finish date (never from
    the start of the preparation).
    shortage_cost: cost of one missing portion relative to the cost of one portion.
    transfer_leftovers: leftovers still valid the day after the cycle are sent to another site
    instead of being counted as waste (Franklin -> MUT on Monday).
    """
    delivery_weekdays: tuple[int, ...]
    shortage_cost: float = 2.0
    transfer_leftovers: bool = False

    def validate(self) -> None:
        weekdays = self.delivery_weekdays
        if (not weekdays or len(set(weekdays)) != len(weekdays)
                or any(type(day) is not int or not 1 <= day <= 7 for day in weekdays)):
            raise ValueError("delivery_weekdays must contain unique ISO weekdays, Monday=1 to Sunday=7")
        if isinstance(self.shortage_cost, bool) or not isinstance(self.shortage_cost, (int, float)) \
                or not 0 < self.shortage_cost <= 100:
            raise ValueError("shortage_cost must be a number in (0, 100]")
        if type(self.transfer_leftovers) is not bool:
            raise ValueError("transfer_leftovers must be a boolean")


@dataclass(frozen=True)
class InventorySettings:
    """Global options of the inventory model (scripts/modelo_inventario.R)."""
    enabled: bool = True
    confidence_level: float = 0.95
    default_service_level: float = 0.95
    min_fractile: float = 0.50
    max_fractile: float = 0.99
    output_subdirectory: str = "inventario"
    export_excel: bool = True
    export_parquet: bool = True

    def validate(self) -> None:
        for name in ("enabled", "export_excel", "export_parquet"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        for name in ("confidence_level", "default_service_level", "min_fractile", "max_fractile"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
                raise ValueError(f"{name} must be a probability strictly between 0 and 1")
        if self.min_fractile > self.max_fractile:
            raise ValueError("min_fractile cannot exceed max_fractile")
        if not self.export_excel and not self.export_parquet:
            raise ValueError("Enable at least one inventory output format")


INVENTORY_SITE_SETTINGS = {
    # Interviews: MUT receives on Tuesday (covers Tue-Thu) and Friday (covers Fri-Mon);
    # Franklin's production is ready on Thursday for the Friday-Sunday service.
    "FRANKLIN": InventorySiteSettings(delivery_weekdays=(5,), transfer_leftovers=True),
    "MUT": InventorySiteSettings(delivery_weekdays=(2, 5)),
}
INVENTORY_SETTINGS = InventorySettings()


def get_inventory_config(model_settings: dict[str, ModelSettings] | None = None, *,
                         site_settings: dict[str, InventorySiteSettings] | None = None,
                         settings: InventorySettings | None = None,
                         repository_root: str | Path | None = None,
                         parameters_workbook: str | Path | None = None) -> dict:
    """Validated JSON contract of the inventory model for the selected sites.

    The demand settings decide horizon, opening days and model; the inventory model reads the
    forecasts that the demand models wrote for exactly those settings.
    """
    settings = settings if settings is not None else INVENTORY_SETTINGS
    settings.validate()
    sites = tuple(model_settings) if model_settings is not None else tuple(MODEL_SETTINGS)
    if not sites:
        raise ValueError("Select at least one site")
    root = Path(repository_root).resolve() if repository_root is not None else REPOSITORY_ROOT
    workbook = Path(parameters_workbook) if parameters_workbook is not None \
        else REPOSITORY_ROOT / INVENTORY_PARAMETERS_WORKBOOK
    entries = []
    for site in sites:
        site = site.upper()
        demand = get_model_config(site, repository_root=root,
                                  settings=model_settings[site] if model_settings is not None else None)
        if not demand["export_excel"]:
            raise ValueError(f"The inventory model reads the demand workbook: enable Excel output for {site}")
        replenishment = (site_settings or {}).get(site, INVENTORY_SITE_SETTINGS[site])
        replenishment.validate()
        entries.append({
            "site": site, **asdict(replenishment),
            "operating_weekdays": demand["operating_weekdays"],
            "horizon_days": demand["horizon_days"], "demand_model": demand["model"],
            "demand_workbook": f"{demand['output_directory']}/{demand['workbook_filename']}",
        })
    subdirectory = Path(settings.output_subdirectory)
    if not settings.output_subdirectory or subdirectory.is_absolute() or ".." in subdirectory.parts:
        raise ValueError("output_subdirectory must be a relative directory under out")
    output = (root / "out" / subdirectory).resolve()
    if not output.is_relative_to((root / "out").resolve()) or output == (root / "out").resolve():
        raise ValueError("Inventory output must stay inside a subdirectory of out")
    packages = list(INVENTORY_R_PACKAGES)
    if not settings.export_excel:
        packages.remove("writexl")
    if settings.export_parquet:
        packages.append("arrow")
    return {
        **asdict(settings), "schema_version": 1, "model": "inventory",
        "repository_root": root.as_posix(), "sites": entries,
        "parameters_workbook": workbook.resolve().as_posix(),
        "parameter_sheets": {name: list(columns) for name, columns in INVENTORY_PARAMETER_SHEETS.items()},
        "output_directory": output.as_posix(), "workbook_filename": "modelo_inventario.xlsx",
        "required_r_packages": packages,
    }


# --- Editable defaults ---------------------------------------------------------------------
# The constants above are the code defaults. The interface's "Valores por defecto" view saves
# overrides in settings/valores_por_defecto.json; the interface and main.py start from them.
USER_DEFAULTS_FILE = "settings/valores_por_defecto.json"
GENERAL_DEFAULT_FIELDS = ("horizon_days", "generate_plots", "export_parquet", "history_plot_days",
                          "residual_min_observations")
SITE_MODEL_DEFAULT_FIELDS = ("operating_weekdays", "model")
SITE_INVENTORY_DEFAULT_FIELDS = ("delivery_weekdays", "shortage_cost", "transfer_leftovers")
# Keys that older saved files may contain and that are now ignored.
OBSOLETE_DEFAULT_FIELDS = {"preparation_lead_days"}
INVENTORY_DEFAULT_FIELDS = ("enabled", "confidence_level", "default_service_level")
_TUPLE_FIELDS = {"operating_weekdays", "delivery_weekdays"}


@dataclass(frozen=True)
class Defaults:
    """Effective starting values of the interface and of main.py."""
    model_settings: dict[str, ModelSettings]
    inventory_sites: dict[str, InventorySiteSettings]
    inventory: InventorySettings

    def validate(self) -> None:
        if set(self.model_settings) != set(MODEL_SETTINGS) or set(self.inventory_sites) != set(MODEL_SETTINGS):
            raise ValueError(f"Defaults must cover exactly the sites {', '.join(MODEL_SETTINGS)}")
        for site, settings in self.model_settings.items():
            get_model_config(site, settings=settings)
        general = {name: [getattr(s, name) for s in self.model_settings.values()] for name in GENERAL_DEFAULT_FIELDS}
        if any(len(set(values)) != 1 for values in general.values()):
            raise ValueError("Horizon, plots, Parquet and residual options must be equal for every site")
        for settings in self.inventory_sites.values():
            settings.validate()
        self.inventory.validate()


def code_defaults() -> Defaults:
    return Defaults(dict(MODEL_SETTINGS), dict(INVENTORY_SITE_SETTINGS), INVENTORY_SETTINGS)


def defaults_to_dict(defaults: Defaults) -> dict:
    defaults.validate()
    first = next(iter(defaults.model_settings.values()))
    return {
        "schema_version": 1,
        "general": {name: getattr(first, name) for name in GENERAL_DEFAULT_FIELDS},
        "sites": {site: {**{name: getattr(defaults.model_settings[site], name) for name in SITE_MODEL_DEFAULT_FIELDS},
                         **{name: getattr(defaults.inventory_sites[site], name) for name in SITE_INVENTORY_DEFAULT_FIELDS}}
                  for site in MODEL_SETTINGS},
        "inventory": {name: getattr(defaults.inventory, name) for name in INVENTORY_DEFAULT_FIELDS},
    }


def defaults_from_dict(data: dict) -> Defaults:
    """Rebuild validated defaults from their JSON form; unknown keys are rejected."""
    def values(section: dict, allowed: tuple[str, ...], label: str) -> dict:
        if not isinstance(section, dict):
            raise ValueError(f"'{label}' must be an object")
        section = {name: value for name, value in section.items() if name not in OBSOLETE_DEFAULT_FIELDS}
        unknown = set(section) - set(allowed)
        if unknown:
            raise ValueError(f"Unknown keys in '{label}': {', '.join(sorted(unknown))}")
        out = {}
        for name, value in section.items():
            if name in _TUPLE_FIELDS:
                if not isinstance(value, list):
                    raise ValueError(f"'{label}.{name}' must be a list of weekdays")
                value = tuple(value)
            elif name == "shortage_cost" and type(value) is int:
                value = float(value)
            out[name] = value
        return out

    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported defaults file: schema_version must be 1")
    unknown = set(data) - {"schema_version", "general", "sites", "inventory"}
    if unknown:
        raise ValueError(f"Unknown keys: {', '.join(sorted(unknown))}")
    general = values(data.get("general", {}), GENERAL_DEFAULT_FIELDS, "general")
    sites = data.get("sites", {})
    if not isinstance(sites, dict) or set(sites) - set(MODEL_SETTINGS):
        raise ValueError(f"'sites' may only contain {', '.join(MODEL_SETTINGS)}")
    model_settings, inventory_sites = {}, {}
    for site in MODEL_SETTINGS:
        site_values = values(sites.get(site, {}), SITE_MODEL_DEFAULT_FIELDS + SITE_INVENTORY_DEFAULT_FIELDS,
                             f"sites.{site}")
        model_settings[site] = replace(MODEL_SETTINGS[site], **general,
                                       **{k: v for k, v in site_values.items() if k in SITE_MODEL_DEFAULT_FIELDS})
        inventory_sites[site] = replace(INVENTORY_SITE_SETTINGS[site],
                                        **{k: v for k, v in site_values.items() if k in SITE_INVENTORY_DEFAULT_FIELDS})
    inventory = replace(INVENTORY_SETTINGS, **values(data.get("inventory", {}), INVENTORY_DEFAULT_FIELDS, "inventory"))
    defaults = Defaults(model_settings, inventory_sites, inventory)
    defaults.validate()
    return defaults


def user_defaults_path(repository_root: str | Path | None = None) -> Path:
    return (Path(repository_root) if repository_root is not None else REPOSITORY_ROOT) / USER_DEFAULTS_FILE


def load_user_defaults(repository_root: str | Path | None = None) -> Defaults:
    """Saved defaults, or the code defaults when nothing was saved. Invalid files raise ValueError."""
    path = user_defaults_path(repository_root)
    if not path.is_file():
        return code_defaults()
    try:
        return defaults_from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{path.name} is invalid: {exc}") from exc


def save_user_defaults(defaults: Defaults, repository_root: str | Path | None = None) -> Path:
    path = user_defaults_path(repository_root)
    content = json.dumps(defaults_to_dict(defaults), ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return path


def reset_user_defaults(repository_root: str | Path | None = None) -> None:
    user_defaults_path(repository_root).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export demand- or inventory-model settings as JSON for R")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--site", choices=tuple(MODEL_SETTINGS))
    target.add_argument("--inventory", action="store_true", help="Default inventory configuration")
    args = parser.parse_args()
    try:
        defaults = load_user_defaults()  # values saved from the interface, or the code defaults
        config = (get_inventory_config(defaults.model_settings, site_settings=defaults.inventory_sites,
                                       settings=defaults.inventory)
                  if args.inventory else get_model_config(args.site, settings=defaults.model_settings[args.site]))
        print(json.dumps(config, ensure_ascii=True, allow_nan=False))
    except (TypeError, ValueError, OSError) as exc:
        print(f"Invalid model configuration: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
