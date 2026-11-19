"""Production report (PDF) for the central kitchen that supplies every location.

The kitchen produces in two cycles (Friday-Monday and Tuesday-Thursday) for MUT, Franklin and the
Franklin store.
For every group of dishes (settings/reporte_grupos.xlsx) the report states how many dishes to
produce per cycle, how many to dispatch to each location, and whether demand is rising or falling.

Production quantity, per location x variant x cycle (same principle as the inventory model):
    producir = ceil( sum(forecast) + z * sqrt(sum(sigma^2)) )
with z taken from the service level (default 95%), days treated as independent. Totals are sums of
the rounded figures, so every table adds up.

Products without sales in the last REPORT_INACTIVE_DAYS days of a site's data are treated as
discontinued or renamed: they are not produced and are listed at the end of the report.

Traffic light, per location x group: forecast for the report week vs actual sales of the last
seven days of data. |change| < 15% green "Estable", up to 30% yellow, above 30% red; the arrow
and sign give the direction.
"""
from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Iterable

import polars as pl

from settings.config import (REPORT_CYCLES, REPORT_EXCLUDED_GROUP, REPORT_GROUPS_COLUMNS, REPORT_GROUPS_SHEET,
                             REPORT_INACTIVE_DAYS, REPORT_MIN_HISTORY_DAYS, REPORT_OUTPUT_SUBDIRECTORY, REPORT_SITES,
                             REPORT_STALE_DATA_DAYS, REPORT_TREND_THRESHOLDS, REPORT_WEEK_START, SITE_LABELS)

STATUS_FILE = "estado.json"
WEEKDAYS_SHORT = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")

def sites_text(sites) -> str:
    """'MUT, Franklin y Tienda Franklin'."""
    names = [SITE_LABELS[site] for site in sites]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " y " + names[-1]


class ReportError(Exception):
    """A problem the user can fix (stale data, malformed mapping...). Message in Spanish."""


# --------------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------------
def normalize_name(value) -> str:
    """Accent-, case- and spacing-insensitive product key ("KARAAGE KIDS " == "Karaage kids", "Maze_Udon")."""
    text = unicodedata.normalize("NFKD", str(value or "").replace("_", " "))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass(frozen=True)
class GroupRule:
    group: str
    variant: str
    note: str = ""
    rank: int = 0          # row in the mapping sheet: variants are listed in sheet order


@dataclass(frozen=True)
class GroupMapping:
    groups: tuple[str, ...]                       # page order
    rules: dict[tuple[str, str], GroupRule]       # (site, normalized product) -> rule (None group = excluded)

    def lookup(self, site: str, product: str) -> GroupRule | None:
        return self.rules.get((site, normalize_name(product)))


def read_group_mapping(path: Path, sites: Iterable[str] = REPORT_SITES) -> GroupMapping:
    """Validate settings/reporte_grupos.xlsx and return the page order and the product rules."""
    from openpyxl import load_workbook
    sites = tuple(sites)
    if not path.is_file():
        raise ReportError(f"Falta el Excel de grupos del reporte: {path.name}.")
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise ReportError(f"No se pudo abrir {path.name}: {exc}") from exc
    try:
        if REPORT_GROUPS_SHEET not in workbook.sheetnames:
            raise ReportError(f"Al Excel {path.name} le falta la hoja «{REPORT_GROUPS_SHEET}».")
        values = [row for row in workbook[REPORT_GROUPS_SHEET].iter_rows(values_only=True)
                  if any(cell not in (None, "") for cell in row)]
    finally:
        workbook.close()
    header = [str(cell).strip().lower() if cell is not None else "" for cell in (values[0] if values else ())]
    missing = [column for column in REPORT_GROUPS_COLUMNS if column not in header]
    if missing:
        raise ReportError(f"La hoja «{REPORT_GROUPS_SHEET}» necesita las columnas: {', '.join(missing)}.")
    position = {column: header.index(column) for column in REPORT_GROUPS_COLUMNS}

    def cell(row, column) -> str:
        index = position[column]
        value = row[index] if index < len(row) else None
        return "" if value is None else str(value).strip()

    groups: list[str] = []
    rules: dict[tuple[str, str], GroupRule] = {}
    scope: dict[tuple[str, str], str] = {}          # where each rule came from: a site or AMBOS
    for number, row in enumerate(values[1:], 2):
        site, product, group = cell(row, "local").upper(), cell(row, "producto"), cell(row, "grupo")
        if site not in {*sites, "AMBOS"}:
            raise ReportError(f"{REPORT_GROUPS_SHEET}, fila {number}: local debe ser {', '.join(sites)} o AMBOS.")
        if not product or not group:
            raise ReportError(f"{REPORT_GROUPS_SHEET}, fila {number}: faltan producto o grupo.")
        excluded = normalize_name(group) == normalize_name(REPORT_EXCLUDED_GROUP)
        rule = GroupRule("" if excluded else group, cell(row, "variante") or product, cell(row, "nota"), number)
        if not excluded and group not in groups:
            groups.append(group)
        # A row for one site overrides the AMBOS row of the same product, whatever the row order
        # (same rule as the inventory "composicion" sheet); the same scope twice is an error.
        for target in (sites if site == "AMBOS" else (site,)):
            key = (target, normalize_name(product))
            if key in rules and scope[key] == site:
                where = "AMBOS" if site == "AMBOS" else target
                raise ReportError(f"{REPORT_GROUPS_SHEET}, fila {number}: «{product}» ya está asignado en {where}.")
            if key in rules and site == "AMBOS":
                continue                            # the site-specific row stays
            rules[key], scope[key] = rule, site
    if not groups:
        raise ReportError(f"La hoja «{REPORT_GROUPS_SHEET}» no tiene ningún grupo para el reporte.")
    return GroupMapping(tuple(groups), rules)


def _read_sheet(workbook_path: Path, sheet: str) -> pl.DataFrame:
    from openpyxl import load_workbook
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        if sheet not in workbook.sheetnames:
            raise ReportError(f"{workbook_path.name} no tiene la hoja «{sheet}».")
        rows = list(workbook[sheet].iter_rows(values_only=True))
    finally:
        workbook.close()
    if not rows:
        return pl.DataFrame()
    header = [str(name) for name in rows[0]]
    columns = {name: [row[i] if i < len(row) else None for row in rows[1:]] for i, name in enumerate(header)}
    return pl.DataFrame(columns, strict=False)


def _as_date(frame: pl.DataFrame) -> pl.DataFrame:
    dtype = frame.schema["fecha"]
    if dtype == pl.Date:
        return frame
    if dtype == pl.Datetime or isinstance(dtype, pl.Datetime):
        return frame.with_columns(pl.col("fecha").dt.date())
    return frame.with_columns(pl.col("fecha").cast(pl.Utf8).str.slice(0, 10).str.to_date())


def read_site_outputs(output_directory: Path, workbook_filename: str) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Forecast (fecha, producto, mu, varianza) and history (fecha, producto, observado) of one site.

    Reads whatever the latest run wrote. Output folders are reused between runs and old files are
    not deleted, so Parquet left by an earlier run must not win over a newer Excel workbook: the
    run's config_used.json says whether Parquet was exported; without it, the newer files win.
    """
    forecast_path, history_path = output_directory / "pronosticos.parquet", output_directory / "historico.parquet"
    workbook = output_directory / workbook_filename
    parquet = forecast_path.is_file() and history_path.is_file()
    if parquet and workbook.is_file():
        try:
            used = json.loads((output_directory / "config_used.json").read_text(encoding="utf-8"))
            parquet = bool(used["export_parquet"])
        except (OSError, ValueError, KeyError, TypeError):
            parquet = min(forecast_path.stat().st_mtime, history_path.stat().st_mtime) >= workbook.stat().st_mtime
    if parquet:
        forecast, history = pl.read_parquet(forecast_path), pl.read_parquet(history_path)
    elif workbook.is_file():
        forecast, history = _read_sheet(workbook, "Pronosticos"), _read_sheet(workbook, "Historico")
    else:
        raise ReportError(f"No se encontraron los resultados del pronóstico en {output_directory}.")
    return prepare_outputs(forecast, history, output_directory.name)


def prepare_outputs(forecast: pl.DataFrame, history: pl.DataFrame,
                    label: str = "") -> tuple[pl.DataFrame, pl.DataFrame]:
    """Model tables (Pronosticos, Historico) -> the columns the report uses; negative forecasts count as 0.

    Unknown spread (a product with a single observation has no sigma) never means zero uncertainty:
    the Poisson approximation for counts is used instead, variance = expected demand.
    """
    for name, frame, columns in (("Pronosticos", forecast, ("fecha", "producto", "demanda_predicha")),
                                 ("Historico", history, ("fecha", "producto", "observado"))):
        missing = [column for column in columns if column not in frame.columns]
        if missing:
            raise ReportError(f"La tabla {name} de {label} no tiene: {', '.join(missing)}.")
    sigma = [column for column in ("sigma_utilizada", "sigma_global") if column in forecast.columns]
    mu = pl.col("demanda_predicha").cast(pl.Float64).fill_nan(None).fill_null(0).clip(lower_bound=0)
    known = [pl.when(pl.col(c).cast(pl.Float64).is_finite()).then(pl.col(c).cast(pl.Float64)) for c in sigma]
    forecast = _as_date(forecast).select(
        "fecha", pl.col("producto").cast(pl.Utf8), mu.alias("mu"),
        pl.coalesce([value ** 2 for value in known] + [mu]).alias("varianza"))
    history = _as_date(history).select("fecha", pl.col("producto").cast(pl.Utf8),
                                       pl.col("observado").cast(pl.Float64).fill_null(0))
    return forecast, history


# --------------------------------------------------------------------------------------------
# Period and cycles
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Block:
    cycle: int            # index in REPORT_CYCLES
    days: tuple[date, ...]

    @property
    def name(self) -> str:
        return REPORT_CYCLES[self.cycle][0]

    @property
    def dates(self) -> str:
        first, last = self.days[0], self.days[-1]
        return f"{first:%d/%m}" if first == last else f"{first:%d/%m}–{last:%d/%m}"


def report_window(today: date, start_today: bool = False) -> tuple[date, ...]:
    """Seven days: from the next Friday after today (default) or from today."""
    if start_today:
        start = today
    else:
        start = today + timedelta(days=(REPORT_WEEK_START - today.isoweekday()) % 7 or 7)
    return tuple(start + timedelta(days=offset) for offset in range(7))


def cycle_of(day: date) -> int:
    return next(index for index, (_label, weekdays) in enumerate(REPORT_CYCLES) if day.isoweekday() in weekdays)


def cycle_blocks(days: tuple[date, ...]) -> tuple[Block, ...]:
    """Consecutive days of the same cycle; a week starting mid-cycle yields three blocks."""
    blocks: list[list[date]] = []
    for day in days:
        if blocks and cycle_of(blocks[-1][-1]) == cycle_of(day):
            blocks[-1].append(day)
        else:
            blocks.append([day])
    return tuple(Block(cycle_of(block[0]), tuple(block)) for block in blocks)


def day_label(day: date) -> str:
    return f"{WEEKDAYS_SHORT[day.weekday()]} {day:%d/%m}"


# --------------------------------------------------------------------------------------------
# Calculations
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Trend:
    forecast: float
    actual: float

    @property
    def change(self) -> float | None:
        # Rounded so that exactly 15% or 30% is not pushed across a threshold by floating point.
        return None if self.actual <= 0 else round(self.forecast / self.actual - 1, 6)

    @property
    def points(self) -> int | None:
        """Change in whole percentage points, as printed; colour and word are decided on this value."""
        change = self.change
        return None if change is None else int(math.copysign(math.floor(abs(change) * 100 + 0.5), change))

    @property
    def level(self) -> str:
        """verde / amarillo / rojo, or sin_base when there were no sales to compare with."""
        if self.points is None:
            return "sin_base"
        low, high = (round(value * 100) for value in REPORT_TREND_THRESHOLDS)
        size = abs(self.points)
        return "verde" if size < low else "amarillo" if size <= high else "rojo"

    @property
    def word(self) -> str:
        if self.points is None:
            return "Sin ventas previas"
        if self.level == "verde":
            return "Estable"
        return "Alza" if self.points > 0 else "Baja"

    @property
    def percent(self) -> str:
        return "" if self.points is None else f"{self.points:+d}%"


@dataclass
class VariantLine:
    variant: str
    produce: list[int]        # per block
    forecast: list[float]     # per block
    few_data: bool = False    # some product of this line has less than REPORT_MIN_HISTORY_DAYS of history

    @property
    def total(self) -> int:
        return sum(self.produce)


@dataclass
class GroupReport:
    name: str
    notes: list[str]
    variants: list[VariantLine]                     # summed over sites
    sites: dict[str, list[VariantLine]]             # site -> variants sold there
    trends: dict[str, Trend | None]                 # None: group not sold at that site
    daily_forecast: list[float]                     # per window day, all sites
    daily_actual: list[float]                       # same weekday in the last 7 days of data

    def produce(self, block: int) -> int:
        return sum(line.produce[block] for line in self.variants)

    def forecast(self, block: int) -> float:
        return sum(line.forecast[block] for line in self.variants)

    @property
    def total(self) -> int:
        return sum(line.total for line in self.variants)

    @property
    def forecast_total(self) -> float:
        return sum(sum(line.forecast) for line in self.variants)


@dataclass
class ReportData:
    generated_at: datetime
    today: date
    days: tuple[date, ...]
    blocks: tuple[Block, ...]
    service_level: float
    last_data: dict[str, date]
    groups: list[GroupReport]
    unmapped: list[tuple[str, str, float]] = field(default_factory=list)   # site, product, week forecast
    inactive: list[tuple[str, str, float, date | None]] = field(default_factory=list)  # ... and last sale

    @property
    def stale_sites(self) -> list[str]:
        return [site for site, last in self.last_data.items() if (self.today - last).days > REPORT_STALE_DATA_DAYS]

    @property
    def week_label(self) -> str:
        return f"{day_label(self.days[0])} – {day_label(self.days[-1])}"


def production_quantity(mu: float, variance: float, z: float) -> int:
    """Dishes covering the service level; nothing when no demand is expected."""
    if mu <= 0:
        return 0
    return math.ceil(mu + z * math.sqrt(max(variance, 0.0)) - 1e-9)


def _missing_open_days(forecast: pl.DataFrame, days: tuple[date, ...]) -> list[date]:
    """Days of the week the site is open (weekdays it has forecasts for) but has no forecast.

    Closed days need nothing: Franklin's forecast starts on Friday even when the week starts today.
    """
    open_days = set(forecast["fecha"].dt.weekday().unique().to_list())
    forecast_days = set(forecast["fecha"].unique().to_list())
    return [day for day in days if day.isoweekday() in open_days and day not in forecast_days]


def build_report(outputs: dict[str, tuple[pl.DataFrame, pl.DataFrame]], mapping: GroupMapping, *,
                 service_level: float, today: date, start_today: bool = False,
                 generated_at: datetime | None = None) -> ReportData:
    """Aggregate the forecasts of every site into the per-group figures shown in the PDF."""
    if not 0 < service_level < 1:
        raise ReportError("El nivel de servicio debe estar entre 0 y 1.")
    z = NormalDist().inv_cdf(service_level)
    days = report_window(today, start_today)
    blocks = cycle_blocks(days)
    block_of = {day: index for index, block in enumerate(blocks) for day in block.days}
    sites = [site for site in REPORT_SITES if site in outputs]

    rows, actual_rows, last_data, unmapped, inactive = [], [], {}, [], []
    for site in sites:
        forecast, history = outputs[site]
        if forecast.is_empty():
            raise ReportError(f"El pronóstico de {SITE_LABELS[site]} está vacío.")
        last = history["fecha"].max() if not history.is_empty() else forecast["fecha"].min() - timedelta(days=1)
        last_data[site] = last
        missing = _missing_open_days(forecast, days)
        if missing:
            first, until = forecast["fecha"].min(), forecast["fecha"].max()
            listed = ", ".join(f"{day:%d/%m}" for day in missing)
            if missing[0] <= last:
                raise ReportError(
                    f"{SITE_LABELS[site]} ya tiene ventas registradas hasta el {last:%d/%m/%Y}, así que no hay "
                    f"pronóstico para {listed}. Desactiva «comenzar hoy» para reportar la próxima semana.")
            raise ReportError(
                f"El pronóstico de {SITE_LABELS[site]} cubre del {first:%d/%m/%Y} al {until:%d/%m/%Y} y le faltan "
                f"días abiertos de la semana del reporte ({listed}). Carga ventas más recientes "
                f"(hay datos hasta el {last:%d/%m/%Y}) o aumenta los días a proyectar.")
        week = forecast.filter(pl.col("fecha").is_in(days))
        recent = history.filter(pl.col("fecha") > last - timedelta(days=7))
        sold = history.filter(pl.col("observado") > 0)
        active = set(sold.filter(pl.col("fecha") > last - timedelta(days=REPORT_INACTIVE_DAYS))["producto"].to_list())
        last_sale = dict(sold.group_by("producto").agg(pl.col("fecha").max()).iter_rows())
        first_sale = dict(sold.group_by("producto").agg(pl.col("fecha").min()).iter_rows())
        for product in sorted(set(week["producto"].to_list()) | set(recent["producto"].to_list())):
            rule = mapping.lookup(site, product)
            units = float(week.filter(pl.col("producto") == product)["mu"].sum())
            if product not in active:
                # Discontinued or renamed: the model still forecasts it from older sales.
                if units >= 0.5 and (rule is None or rule.group):
                    inactive.append((site, product.strip(), units, last_sale.get(product)))
            elif rule is None:
                unmapped.append((site, product.strip(), units))
            elif rule.group:
                started = first_sale.get(product)
                new = started is None or (last - started).days + 1 < REPORT_MIN_HISTORY_DAYS
                for day, mu, variance in week.filter(pl.col("producto") == product).select(
                        "fecha", "mu", "varianza").iter_rows():
                    rows.append((site, rule, day, mu, variance, new))
                for day, observed in recent.filter(pl.col("producto") == product).select(
                        "fecha", "observado").iter_rows():
                    actual_rows.append((site, rule.group, day, observed))

    groups = []
    for name in mapping.groups:
        group_rows = [row for row in rows if row[1].group == name]
        group_actual = [row for row in actual_rows if row[1] == name]
        if not group_rows and not group_actual:
            continue
        ranks: dict[str, int] = {}
        for row in group_rows:
            ranks[row[1].variant] = min(ranks.get(row[1].variant, row[1].rank), row[1].rank)
        order = sorted(ranks, key=ranks.get)
        notes = list(dict.fromkeys(row[1].note for row in group_rows if row[1].note))
        per_site: dict[str, list[VariantLine]] = {}
        for site in sites:
            lines = []
            for variant in order:
                selected = [r for r in group_rows if r[0] == site and r[1].variant == variant]
                if not selected:
                    continue
                mu = [sum(r[3] for r in selected if block_of[r[2]] == b) for b in range(len(blocks))]
                var = [sum(r[4] for r in selected if block_of[r[2]] == b) for b in range(len(blocks))]
                lines.append(VariantLine(variant, [production_quantity(m, v, z) for m, v in zip(mu, var)], mu,
                                         any(r[5] for r in selected)))
            if lines:
                per_site[site] = lines
        variants = []
        for variant in order:
            parts = [line for lines in per_site.values() for line in lines if line.variant == variant]
            variants.append(VariantLine(variant, [sum(p.produce[b] for p in parts) for b in range(len(blocks))],
                                        [sum(p.forecast[b] for p in parts) for b in range(len(blocks))],
                                        any(p.few_data for p in parts)))
        trends: dict[str, Trend | None] = {}
        for site in sites:
            sold = site in per_site or any(r[0] == site for r in group_actual)
            trends[site] = Trend(sum(r[3] for r in group_rows if r[0] == site),
                                 sum(r[3] for r in group_actual if r[0] == site)) if sold else None
        actual_by_weekday: dict[int, float] = {}
        for _site, _group, day, observed in group_actual:
            actual_by_weekday[day.weekday()] = actual_by_weekday.get(day.weekday(), 0.0) + observed
        groups.append(GroupReport(
            name, notes, variants, per_site, trends,
            daily_forecast=[sum(r[3] for r in group_rows if r[2] == day) for day in days],
            daily_actual=[actual_by_weekday.get(day.weekday(), 0.0) for day in days]))
    if not groups:
        raise ReportError("Ningún producto vendido pertenece a los grupos del reporte. Revisa reporte_grupos.xlsx.")
    unmapped.sort(key=lambda item: (-item[2], item[0], item[1]))
    inactive.sort(key=lambda item: (-item[2], item[0], item[1]))
    return ReportData(generated_at or datetime.now(), today, days, blocks, service_level, last_data, groups,
                      unmapped, inactive)


# --------------------------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------------------------
def render_pdf(data: ReportData, path: Path) -> Path:
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Circle, Drawing, Polygon, Rect, String
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import (CondPageBreak, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    crimson, coral = colors.HexColor("#A82D2F"), colors.HexColor("#E86567")
    blush, ink, soft = colors.HexColor("#F7F0F1"), colors.HexColor("#111827"), colors.HexColor("#4B5563")
    line, gray = colors.HexColor("#E5E7EB"), colors.HexColor("#B8BEC6")
    light = {"verde": colors.HexColor("#1F9D55"), "amarillo": colors.HexColor("#E3A008"),
             "rojo": colors.HexColor("#D1383A"), "sin_base": colors.HexColor("#9CA3AF")}
    cycle_colors = [crimson, coral]
    width = A4[0] - 30 * mm

    def style(name, parent=None, **kw):
        if parent is not None:          # inherit everything not given explicitly
            return ParagraphStyle(name, parent=parent, **kw)
        base = dict(fontName="Helvetica", fontSize=9, leading=12, textColor=ink)
        base.update(kw)
        return ParagraphStyle(name, **base)

    title = style("title", fontName="Helvetica-Bold", fontSize=30, leading=33, textColor=crimson)
    subtitle = style("subtitle", fontSize=11, leading=15, textColor=soft)
    heading = style("heading", fontName="Helvetica-Bold", fontSize=11, leading=14, spaceBefore=10, spaceAfter=5)
    body = style("body")
    small = style("small", fontSize=7.5, leading=10, textColor=soft)
    cell = style("cell", fontSize=8.5, leading=10.5)
    head_cell = style("headcell", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white)
    head_num = style("headnum", parent=head_cell, alignment=TA_CENTER)

    def number(value: float) -> str:
        return f"{value:,.0f}".replace(",", ".")

    def badge(trend: Trend | None, wide: bool = False, space: float = 96) -> Drawing:
        """Traffic light: coloured dot, arrow, word and percentage. Readable in black and white.

        space: width available in the table cell; the text shrinks so it never runs into the next column.
        """
        width_available = 150 if wide else space
        drawing = Drawing(width_available, 14)
        if trend is None:
            drawing.add(String(2, 3, "no se vende", fontName="Helvetica", fontSize=min(7.5, width_available / 8),
                               fillColor=soft))
            return drawing
        color = light[trend.level]
        drawing.add(Circle(6, 7, 5, fillColor=color, strokeColor=color))
        change = trend.change
        if trend.level in ("amarillo", "rojo"):
            points = (15, 3, 23, 3, 19, 11) if trend.points > 0 else (15, 11, 23, 11, 19, 3)
            drawing.add(Polygon(points, fillColor=ink, strokeColor=ink, strokeWidth=0))
        else:
            drawing.add(Rect(15, 6, 8, 2, fillColor=ink, strokeColor=ink, strokeWidth=0))
        text = f"{trend.word} {trend.percent}".strip()
        if wide and change is not None:
            text += f"  ({number(trend.forecast)} vs {number(trend.actual)})"
        size = 8.0
        while size > 5.5 and stringWidth(text, "Helvetica-Bold", size) > width_available - 29:
            size -= 0.25
        drawing.add(String(27, 3, text, fontName="Helvetica-Bold", fontSize=size, fillColor=ink))
        return drawing

    def table(rows, columns, *, header_rows=1, bold_rows=(), shade_rows=()):
        result = Table(rows, colWidths=columns, repeatRows=header_rows, hAlign="LEFT")
        commands = [
            ("BACKGROUND", (0, 0), (-1, header_rows - 1), crimson),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("LINEBELOW", (0, header_rows), (-1, -1), 0.4, line),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        for index in bold_rows:
            commands.append(("FONTNAME", (0, index), (-1, index), "Helvetica-Bold"))
        for index in shade_rows:
            commands.append(("BACKGROUND", (0, index), (-1, index), blush))
        result.setStyle(TableStyle(commands))
        return result

    def label(line: VariantLine) -> str:
        return f"{line.variant} *" if line.few_data else line.variant

    def block_header(block: Block) -> Paragraph:
        return Paragraph(f"{block.name}<br/><font size=7>{block.dates}</font>", head_num)

    def big_numbers(group: GroupReport) -> Table:
        count = len(data.blocks)
        widths = [width * 0.72 / count] * count + [width * 0.28]
        label = style("biglabel", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=colors.white,
                      alignment=TA_CENTER)
        big = style("big", fontName="Helvetica-Bold", fontSize=40, leading=44, textColor=colors.white,
                    alignment=TA_CENTER)
        note = style("bignote", fontSize=8, leading=10, textColor=colors.white, alignment=TA_CENTER)
        cells = []
        for index, block in enumerate(data.blocks):
            cells.append([Paragraph(f"PRODUCIR {block.name.upper()} · {block.dates}", label),
                          Paragraph(number(group.produce(index)), big),
                          Paragraph(f"pronóstico {number(group.forecast(index))}", note)])
        total_label = style("tl", parent=label, textColor=crimson)
        total_big = style("tb", parent=big, textColor=crimson, fontSize=28, leading=32)
        total_note = style("tn", parent=note, textColor=soft)
        cells.append([Paragraph("TOTAL SEMANA", total_label), Paragraph(number(group.total), total_big),
                      Paragraph(f"pronóstico {number(group.forecast_total)}", total_note)])
        result = Table([cells], colWidths=widths, hAlign="LEFT")
        commands = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 10),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 10), ("BOX", (-1, 0), (-1, 0), 1.2, crimson),
                    ("BACKGROUND", (-1, 0), (-1, 0), colors.white)]
        for index, block in enumerate(data.blocks):
            commands.append(("BACKGROUND", (index, 0), (index, 0), cycle_colors[block.cycle % 2]))
            commands.append(("LINEAFTER", (index, 0), (index, 0), 3, colors.white))
        result.setStyle(TableStyle(commands))
        return result

    def chart(group: GroupReport) -> Drawing:
        height = 150
        drawing = Drawing(width, height + 30)
        bars = VerticalBarChart()
        bars.x, bars.y, bars.width, bars.height = 28, 22, width - 40, height - 10
        bars.data = [group.daily_actual, group.daily_forecast]
        bars.categoryAxis.categoryNames = [day_label(day) for day in data.days]
        bars.categoryAxis.labels.fontName, bars.categoryAxis.labels.fontSize = "Helvetica", 7.5
        bars.categoryAxis.strokeColor = line
        bars.valueAxis.valueMin = 0
        bars.valueAxis.labels.fontName, bars.valueAxis.labels.fontSize = "Helvetica", 7
        bars.valueAxis.strokeColor = line
        bars.valueAxis.gridStrokeColor, bars.valueAxis.visibleGrid = line, True
        bars.groupSpacing, bars.barSpacing = 10, 1
        bars.bars.strokeWidth = 0
        bars.bars[0].fillColor = gray
        for index, day in enumerate(data.days):
            bars.bars[(1, index)].fillColor = cycle_colors[cycle_of(day) % 2]
        bars.barLabelFormat = "values"
        bars.barLabelArray = [["" for _ in data.days], [number(value) for value in group.daily_forecast]]
        bars.barLabels.fontName, bars.barLabels.fontSize, bars.barLabels.nudge = "Helvetica-Bold", 7, 6
        drawing.add(bars)
        x = 28
        for color, text in ((cycle_colors[0], f"Pronóstico {REPORT_CYCLES[0][0]}"),
                            (cycle_colors[1], f"Pronóstico {REPORT_CYCLES[1][0]}"),
                            (gray, "Ventas reales, mismo día de los últimos 7 días de datos")):
            drawing.add(Rect(x, height + 18, 8, 8, fillColor=color, strokeColor=color))
            drawing.add(String(x + 11, height + 19, text, fontName="Helvetica", fontSize=7.5, fillColor=soft))
            x += 11 + len(text) * 3.9 + 14
        return drawing

    def decorate(canvas, document):
        canvas.saveState()
        page_width, page_height = A4
        canvas.setFillColor(crimson)
        canvas.rect(0, page_height - 11 * mm, page_width, 11 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 8.5)
        canvas.drawString(15 * mm, page_height - 7 * mm, "MIRAI · REPORTE DE PRODUCCIÓN · COCINA CENTRAL")
        canvas.setFont("Helvetica", 8.5)
        canvas.drawRightString(page_width - 15 * mm, page_height - 7 * mm, f"Semana {data.week_label}")
        canvas.setFillColor(soft)
        canvas.setFont("Helvetica", 6.8)
        data_through = " · ".join(f"{SITE_LABELS[s]} {d:%d/%m/%Y}" for s, d in data.last_data.items())
        canvas.drawString(15 * mm, 10 * mm, f"Generado {data.generated_at:%d/%m/%Y %H:%M} · Ventas hasta: "
                                            f"{data_through} · Producir cubre el {data.service_level:.0%} de los escenarios")
        canvas.drawString(15 * mm, 6.5 * mm, "Basado en ventas registradas: los quiebres de stock hacen que la "
                                             "demanda parezca menor de lo que fue.")
        canvas.drawRightString(page_width - 15 * mm, 6.5 * mm, f"Página {document.page}")
        canvas.restoreState()

    story = [Paragraph("REPORTE DE PRODUCCIÓN", title),
             Paragraph(f"Semana {data.week_label} · {sites_text(data.last_data)}", subtitle), Spacer(1, 6)]
    if data.stale_sites:
        listed = "; ".join(f"{SITE_LABELS[site]} hasta el {data.last_data[site]:%d/%m/%Y} "
                           f"({(data.today - data.last_data[site]).days} días)" for site in data.stale_sites)
        warning = Table([[Paragraph(
            f"<b>Atención:</b> las ventas cargadas llegan solo a: {listed}. Carga ventas más recientes para un "
            f"pronóstico confiable.", body)]], colWidths=[width])
        warning.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF3C7")),
                                     ("BOX", (0, 0), (-1, -1), 0.8, light["amarillo"]),
                                     ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        story += [warning, Spacer(1, 6)]

    story.append(Paragraph("Resumen: qué producir", heading))
    sites = list(data.last_data)
    count = len(data.blocks)
    # Narrower number columns leave room for one trend column per site (three with the store).
    columns = [width * 0.17] + [width * 0.11] * count + [width * 0.11]
    trend_width = (width - sum(columns)) / len(sites)
    columns += [trend_width] * len(sites)
    rows = [[Paragraph("Grupo", head_cell), *[block_header(b) for b in data.blocks],
             Paragraph("Total semana", head_num), *[Paragraph(f"Tendencia {SITE_LABELS[s]}", head_cell) for s in sites]]]
    for group in data.groups:
        rows.append([Paragraph(f"<b>{group.name}</b>", cell), *[number(group.produce(i)) for i in range(count)],
                     number(group.total), *[badge(group.trends.get(s), space=trend_width - 12) for s in sites]])
    summary = table(rows, columns)
    summary.setStyle(TableStyle([("FONTNAME", (1, 1), (count, -1), "Helvetica-Bold"),
                                 ("FONTSIZE", (1, 1), (count + 1, -1), 13),
                                 ("TOPPADDING", (0, 1), (-1, -1), 7), ("BOTTOMPADDING", (0, 1), (-1, -1), 7)]))
    story += [summary, Spacer(1, 8)]
    low, high = (f"{value * 100:.0f}%" for value in REPORT_TREND_THRESHOLDS)
    story.append(Paragraph(
        f"<b>Producir</b> = pronóstico más un margen para cubrir el {data.service_level:.0%} de los escenarios, "
        f"calculado por local, variante y ciclo, y redondeado hacia arriba. <b>Tendencia</b>: pronóstico de la "
        f"semana frente a las ventas reales de los últimos 7 días de datos. Verde = Estable (cambio menor a {low}); "
        f"amarillo = cambio entre {low} y {high}; rojo = cambio mayor a {high}. "
        f"La flecha indica Alza o Baja.", small))

    for group in data.groups:
        story += [PageBreak(), Paragraph(group.name.upper(), title),
                  Paragraph(f"Semana {data.week_label}", subtitle), Spacer(1, 8), big_numbers(group), Spacer(1, 10)]
        trend_rows = [[Paragraph(f"<b>{SITE_LABELS[s]}</b>", cell), badge(group.trends.get(s), wide=True)]
                      for s in sites]
        trend_table = Table(trend_rows, colWidths=[width * 0.14, width * 0.5], hAlign="LEFT")
        trend_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                         ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        story += [Paragraph("Tendencia frente a los últimos 7 días de ventas", heading), trend_table]

        columns = [width * 0.4] + [width * 0.45 / count] * count + [width * 0.15]
        header = [Paragraph("Variante", head_cell), *[block_header(b) for b in data.blocks],
                  Paragraph("Total", head_num)]
        rows = [header] + [[Paragraph(label(line), cell), *[number(q) for q in line.produce], number(line.total)]
                           for line in group.variants]
        rows.append([Paragraph("<b>Total</b>", cell), *[number(group.produce(i)) for i in range(count)],
                     number(group.total)])
        production = [Paragraph("Producción por variante (unidades a producir)", heading),
                      table(rows, columns, bold_rows=(len(rows) - 1,), shade_rows=(len(rows) - 1,))]
        if group.notes:
            production.append(Paragraph("Nota: " + "; ".join(group.notes) + ".", small))
        if any(line.few_data for line in group.variants):
            production.append(Paragraph(f"* Menos de {REPORT_MIN_HISTORY_DAYS} días con ventas registradas: el "
                                        "pronóstico es poco confiable (puede reflejar solo el lanzamiento). "
                                        "Revísalo antes de producir.", small))
        story.append(KeepTogether(production))

        rows, bold, shade = [[Paragraph("Local / variante", head_cell), *[block_header(b) for b in data.blocks],
                              Paragraph("Total", head_num)]], [], []
        for site, lines in group.sites.items():
            totals = [sum(line.produce[i] for line in lines) for i in range(count)]
            bold.append(len(rows))
            shade.append(len(rows))
            rows.append([Paragraph(f"<b>{SITE_LABELS[site]}</b>", cell), *map(number, totals), number(sum(totals))])
            rows += [[Paragraph("&nbsp;&nbsp;&nbsp;" + label(line), cell), *[number(q) for q in line.produce],
                      number(line.total)] for line in lines]
        story += [CondPageBreak(60), Paragraph("Despacho por local (unidades a enviar)", heading),
                  table(rows, columns, bold_rows=bold, shade_rows=shade)]
        story += [CondPageBreak(200), Paragraph("Demanda diaria pronosticada", heading), chart(group)]

    if data.unmapped or data.inactive:
        story.append(PageBreak())
    if data.unmapped:
        story += [Paragraph("Productos sin grupo", heading), Paragraph(
            "Estos productos se vendieron o tienen pronóstico, pero no están en settings/reporte_grupos.xlsx, "
            "así que no aparecen en el reporte. Agrégalos a un grupo o márcalos como NO INCLUIR.", body), Spacer(1, 6)]
        rows = [[Paragraph("Local", head_cell), Paragraph("Producto", head_cell),
                 Paragraph("Pronóstico semana", head_num)]]
        rows += [[SITE_LABELS[site], Paragraph(product, cell), number(units)] for site, product, units in data.unmapped]
        story += [table(rows, [width * 0.2, width * 0.55, width * 0.25]), Spacer(1, 12)]
    if data.inactive:
        story += [Paragraph(f"Productos sin ventas en los últimos {REPORT_INACTIVE_DAYS} días (no se producen)", heading),
                  Paragraph("El modelo todavía les asigna un pronóstico por sus ventas antiguas, pero no se vendieron "
                            f"en los últimos {REPORT_INACTIVE_DAYS} días de datos: probablemente salieron de la carta o "
                            "cambiaron de nombre. No se suman a la producción. Si un plato volvió a la carta, aparecerá "
                            "en cuanto registre ventas.", body), Spacer(1, 6)]
        rows = [[Paragraph("Local", head_cell), Paragraph("Producto", head_cell),
                 Paragraph("Pronóstico semana", head_num), Paragraph("Última venta", head_num)]]
        rows += [[SITE_LABELS[site], Paragraph(product, cell), number(units), f"{sold:%d/%m/%Y}" if sold else "—"]
                 for site, product, units, sold in data.inactive]
        story.append(table(rows, [width * 0.15, width * 0.5, width * 0.18, width * 0.17]))

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    document = SimpleDocTemplate(str(temporary), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                                 topMargin=18 * mm, bottomMargin=16 * mm, title="Reporte de producción",
                                 author="MIRAI", subject=f"Semana {data.week_label}")
    document.build(story, onFirstPage=decorate, onLaterPages=decorate)
    temporary.replace(path)
    return path


# --------------------------------------------------------------------------------------------
# Entry point used by main.py and the interface
# --------------------------------------------------------------------------------------------
def write_status(directory: Path, state: str, message: str, file: str | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"estado": state, "mensaje": message, "archivo": file, "fecha": datetime.now().isoformat(timespec="seconds")}
    (directory / STATUS_FILE).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_status(directory: Path) -> dict | None:
    try:
        return json.loads((directory / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def generate_report(workspace: Path, site_outputs: dict[str, tuple[Path, str]], groups_workbook: Path,
                    logger: logging.Logger, *, service_level: float, today: date | None = None,
                    start_today: bool = False) -> Path | None:
    """Write out/reporte/reporte_produccion_<fecha>.pdf and out/reporte/estado.json.

    site_outputs maps each site that ran successfully to (output directory, workbook file name).
    The report needs every site in REPORT_SITES; otherwise it is skipped. Problems with the data are
    logged and recorded in estado.json, but never stop the forecasts that already finished.
    """
    directory = workspace / "out" / REPORT_OUTPUT_SUBDIRECTORY
    missing = [site for site in REPORT_SITES if site not in site_outputs]
    if missing:
        message = (f"El reporte de producción necesita los datos de {sites_text(REPORT_SITES)} "
                   f"en la misma ejecución; faltó {sites_text(missing)}.")
        write_status(directory, "omitido", message)
        logger.info("Production report skipped: %s", message)
        return None
    try:
        mapping = read_group_mapping(groups_workbook)
        outputs = {site: read_site_outputs(Path(output), workbook) for site, (output, workbook) in site_outputs.items()}
        data = build_report(outputs, mapping, service_level=service_level, today=today or date.today(),
                            start_today=start_today)
        path = render_pdf(data, directory / f"reporte_produccion_{data.days[0]:%Y-%m-%d}.pdf")
    except ReportError as exc:
        write_status(directory, "error", str(exc))
        logger.warning("Production report not generated: %s", exc)
        return None
    except Exception as exc:
        write_status(directory, "error", f"No se pudo generar el reporte de producción: {exc}")
        logger.exception("Production report failed")
        return None
    if data.unmapped:
        logger.warning("Production report: %d products have no group in %s", len(data.unmapped), groups_workbook.name)
    if data.inactive:
        logger.info("Production report: %d products without recent sales were not produced", len(data.inactive))
    for site in data.stale_sites:
        logger.warning("Production report: %s sales end on %s", site, data.last_data[site])
    write_status(directory, "generado", f"Reporte de la semana {data.week_label}.", path.name)
    logger.info("Production report saved in %s", path)
    return path
