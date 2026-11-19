"""Run with: python -m streamlit run streamlit_app.py."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from pathlib import Path

import altair as alt
import polars as pl
import streamlit as st

from settings.config import (INVENTORY_PARAMETER_SHEETS, INVENTORY_SETTINGS, MODEL_SETTINGS, REPORT_OUTPUT_SUBDIRECTORY,
                             REPORT_SITES, REPOSITORY_ROOT, SITE_LABELS, WORKBOOK_NAMES,
                             Defaults, code_defaults, load_user_defaults, reset_user_defaults, save_user_defaults,
                             user_defaults_path)
from src.interface import (compare_parameters_workbooks, create_run, default_parameters_workbook, execute_run,
                           read_parameters_workbook, replace_default_parameters, result_zip,
                           validate_parameters_workbook, validate_uploads)
from src.report import read_status
from src.runtime import PipelineError, find_rscript


st.set_page_config(page_title="Mirai · Pronóstico de ventas", page_icon=":material/ramen_dining:", layout="wide")

# --------------------------------------------------------------------------------------------
# Visual identity (presentation only). Palette and typography follow miraifoodlab.cl:
# deep crimson bars and cards, bright red calls to action, coral accents, white and light-gray
# sections, bold uppercase headings with tight tracking and underlined "→" links.
# --------------------------------------------------------------------------------------------
st.markdown("""<style>
:root {
  --mirai-dark: #A82D2F;
  --mirai-red: #D1383A;
  --mirai-coral: #E86567;
  --mirai-blush: #F7F0F1;
  --ink: #111827;
  --ink-soft: #4B5563;
  --line: #E5E7EB;
  --paper: #F9FAFB;
  --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}

.stApp {background: #FFFFFF; color: var(--ink);}
header[data-testid="stHeader"] {background: rgba(255,255,255,.92); backdrop-filter: blur(6px); border-bottom: 1px solid var(--line);}
div[data-testid="stDecoration"] {display: none;}
.block-container {max-width: 1140px; padding-top: 3.2rem; padding-bottom: 2rem;}

/* Headings ---------------------------------------------------------------------------------- */
h1, h2, h3 {font-family: var(--sans) !important; color: var(--ink); font-weight: 700 !important;}
h1 {text-transform: uppercase; letter-spacing: -.025em;}
h2, h3 {letter-spacing: -.02em;}
.stMarkdown p, .stCaption, [data-testid="stCaptionContainer"] {color: var(--ink-soft);}

/* Announcement bar ---------------------------------------------------------------------------- */
.mirai-bar {
  background: var(--mirai-dark); color: #fff; text-align: center; font-size: .85rem; font-weight: 600;
  padding: .55rem 1rem; border-radius: 6px; margin-bottom: 1.25rem;
}
.mirai-bar a {color: #fff !important; margin-left: .75rem; text-decoration: underline; text-underline-offset: 3px;}

/* Hero --------------------------------------------------------------------------------------- */
.mirai-hero {
  position: relative; overflow: hidden; border-radius: 12px; padding: 4.5rem 3rem 3.5rem;
  background:
    radial-gradient(1200px 400px at 85% -10%, rgba(232,101,103,.55), transparent 60%),
    radial-gradient(800px 380px at -10% 120%, rgba(17,24,39,.45), transparent 60%),
    linear-gradient(135deg, #8E2426 0%, var(--mirai-dark) 45%, var(--mirai-red) 100%);
  color: #fff;
}
.mirai-hero::after {               /* soft "noren" stripes, decorative only */
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background: repeating-linear-gradient(90deg, rgba(255,255,255,.045) 0 1px, transparent 1px 140px);
}
.mirai-kicker {font-size: .78rem; font-weight: 600; letter-spacing: .14em; text-transform: uppercase; opacity: .85;}
.mirai-hero h1 {color: #fff !important; font-size: clamp(2.1rem, 4.4vw, 3.4rem) !important; line-height: 1.05;
                margin: .6rem 0 1rem; padding: 0;}
.mirai-hero p {color: rgba(255,255,255,.88) !important; font-size: 1.15rem; max-width: 40rem; margin-bottom: 1.6rem;}
.mirai-link, .mirai-link:visited {
  color: inherit !important; font-weight: 600; font-size: 1.05rem; text-decoration: none;
  border-bottom: 2px solid currentColor; padding-bottom: 2px;
}
.mirai-link::before {content: "→"; margin-right: .6rem; display: inline-block; border: 0;}

/* Section headings ---------------------------------------------------------------------------- */
.mirai-section {scroll-margin-top: 5rem; margin: 3.2rem 0 1.2rem; padding-top: 2.2rem; border-top: 1px solid var(--line);}
.mirai-section .row {display: flex; align-items: center; gap: .9rem;}
.mirai-section [data-testid="stHeaderActionElements"], .mirai-hero [data-testid="stHeaderActionElements"] {display: none;}
.mirai-section .num {
  flex: none; display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem;
  border-radius: 999px; background: var(--mirai-red); color: #fff; font-weight: 700; font-size: .95rem;
}
.mirai-section h2 {text-transform: uppercase; font-size: clamp(1.6rem, 2.8vw, 2.25rem) !important;
                   letter-spacing: -.025em; margin: 0 !important; padding: 0 !important; line-height: 1.1;}
.mirai-section p {color: var(--ink-soft) !important; font-size: 1.1rem; margin: .7rem 0 0;}

/* Site cards (one per local) ------------------------------------------------------------------ */
div[class*="st-key-site-card-"], div.st-key-report-card {
  background: var(--mirai-blush); border: 1px solid #EBDADC !important; border-radius: 12px; padding: .4rem .6rem;
}
div[class*="st-key-site-card-"] h3 {
  color: var(--mirai-dark) !important; text-transform: uppercase; letter-spacing: .02em; font-size: 1.6rem !important;
}
div[class*="st-key-site-card-"] [data-testid="stFileUploaderDropzone"] {background: #fff;}

/* File uploader -------------------------------------------------------------------------------- */
[data-testid="stFileUploaderDropzone"] {
  border: 1.5px dashed var(--mirai-coral) !important; border-radius: 8px; background: #fff;
}

/* Buttons ------------------------------------------------------------------------------------- */
.stButton button, .stDownloadButton button {border-radius: 6px; font-weight: 500; transition: all .15s ease;}
button[kind="primary"], [data-testid="stBaseButton-primary"] {
  background: var(--mirai-red) !important; border-color: var(--mirai-red) !important; color: #fff !important;
}
button[kind="primary"]:hover, [data-testid="stBaseButton-primary"]:hover {
  background: var(--mirai-dark) !important; border-color: var(--mirai-dark) !important;
}
button[kind="primary"]:disabled, [data-testid="stBaseButton-primary"]:disabled {
  background: #F3D3D4 !important; border-color: #F3D3D4 !important; color: #fff !important;
}
.st-key-generate button {padding: .9rem 1rem; font-size: 1.05rem; font-weight: 600; letter-spacing: .02em;}
button[kind="secondary"], [data-testid="stBaseButton-secondary"] {border-color: var(--mirai-dark); color: var(--mirai-dark);}

/* Expanders & bordered blocks ------------------------------------------------------------------ */
[data-testid="stExpander"] details {border: 1px solid var(--line); border-radius: 8px; background: #fff;}
[data-testid="stExpander"] summary {font-weight: 600;}
[data-testid="stExpander"] summary:hover {color: var(--mirai-red);}

/* Metrics as crimson cards (like the locales cards on the site) ---------------------------------- */
div[data-testid="stMetric"] {
  background: var(--mirai-dark); padding: 1.2rem 1.4rem; border-radius: 8px; color: #fff;
}
div[data-testid="stMetric"] * {color: #fff !important;}
div[data-testid="stMetric"] [data-testid="stMetricLabel"] {text-transform: uppercase; letter-spacing: .08em; opacity: .85;}
div[data-testid="stMetric"] [data-testid="stMetricValue"] {font-weight: 700;}

/* Tabs ---------------------------------------------------------------------------------------- */
.stTabs [data-baseweb="tab"] {font-weight: 600; text-transform: uppercase; letter-spacing: .04em;}

/* Sidebar ------------------------------------------------------------------------------------- */
section[data-testid="stSidebar"] {background: #fff; border-right: 1px solid var(--line);}
section[data-testid="stSidebar"] h1 {
  font-size: 1.6rem !important; letter-spacing: .12em; color: var(--mirai-dark) !important; margin-bottom: 0;
}
section[data-testid="stSidebar"] ol {padding-left: 0; list-style: none; counter-reset: paso;}
section[data-testid="stSidebar"] ol li {counter-increment: paso; position: relative; padding-left: 2.2rem; margin-bottom: .7rem;}
section[data-testid="stSidebar"] ol li::before {
  content: counter(paso); position: absolute; left: 0; top: -.05rem; width: 1.5rem; height: 1.5rem;
  border-radius: 999px; background: var(--mirai-blush); color: var(--mirai-dark); font-weight: 700; font-size: .8rem;
  display: flex; align-items: center; justify-content: center;
}

/* Notes: "info" messages take the blush/crimson tone of the site's cards ------------------------ */
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) {
  background: var(--mirai-blush) !important; border-left: 3px solid var(--mirai-coral); color: #7A1F21;
}
[data-testid="stAlertContentInfo"], [data-testid="stAlertContentInfo"] * {color: #7A1F21 !important;}

/* Data-history warning (front page) -------------------------------------------------------- */
.mirai-history {
  display: flex; gap: 1.4rem; align-items: flex-start; margin: 1.6rem 0 .4rem; padding: 1.6rem 1.8rem;
  background: #FEF3C7; border: 2px solid #E3A008; border-left-width: 10px; border-radius: 12px; color: #5B3A06;
}
.mirai-history .icon {flex: none; font-size: 3rem; line-height: 1;}
.mirai-history h2 {font-size: clamp(1.4rem, 2.6vw, 2rem) !important; text-transform: uppercase; letter-spacing: -.02em;
                   color: #5B3A06 !important; margin: 0 0 .6rem !important; padding: 0 !important; line-height: 1.15;}
.mirai-history ul {margin: 0; padding-left: 1.1rem;}
.mirai-history li {font-size: 1.05rem; margin-bottom: .3rem; color: #5B3A06;}
.mirai-history [data-testid="stHeaderActionElements"] {display: none;}

/* Footer -------------------------------------------------------------------------------------- */
.mirai-footer {
  margin-top: 4rem; padding: 2rem 0 .5rem; border-top: 1px solid var(--line); text-align: center;
  color: #334155; font-size: .85rem;
}
.mirai-footer strong {letter-spacing: .14em; text-transform: uppercase; font-size: .78rem; display: block; margin-bottom: .3rem;}
</style>""", unsafe_allow_html=True)

WEEKDAYS = {1: "Lunes", 2: "Martes", 3: "Miércoles", 4: "Jueves", 5: "Viernes", 6: "Sábado", 7: "Domingo"}
MODELS = {"dia": "Por día de la semana", "dia_mes_anual": "Por día de la semana y mes"}
DISPATCH_HELP = ("Días en que se despacha un pedido al local y en que lo despachado queda disponible ahí. Cada despacho "
                 "cubre los días de apertura hasta el siguiente. Las preparaciones quedan terminadas el día de despacho "
                 "y su vida útil se cuenta desde ese término, no desde su inicio.")


def section(number: int, title: str, subtitle: str | None = None, anchor: str | None = None) -> None:
    """Numbered section heading in the restaurant's style (presentation only)."""
    anchor_attr = f' id="{anchor}"' if anchor else ""
    lead = f"<p>{subtitle}</p>" if subtitle else ""
    st.markdown(f'<div class="mirai-section"{anchor_attr}><div class="row"><span class="num">{number}</span>'
                f'<h2>{title}</h2></div>{lead}</div>', unsafe_allow_html=True)


class LiveLog(logging.Handler):
    def __init__(self, placeholder):
        super().__init__()
        self.placeholder = placeholder
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))
        self.placeholder.code("\n".join(self.lines[-18:]), language=None)


def ordered_options(frame: pl.DataFrame, name: str, quantity: str, key: str) -> list[str]:
    """Options sorted by total quantity (largest first) or, with the toggle on, alphabetically."""
    alphabetical = st.toggle("Orden alfabético", key=f"alphabetical-{key}",
                             help="Por defecto la lista se ordena de mayor a menor cantidad proyectada.")
    totals = frame.group_by(name).agg(pl.col(quantity).sum())
    totals = (totals.sort(pl.col(name).str.to_lowercase()) if alphabetical
              else totals.sort([quantity, name], descending=[True, False]))
    return totals[name].to_list()


def forecast_chart(selected: pl.DataFrame) -> alt.Chart:
    """Daily forecast whose line breaks wherever a calendar day has no forecast (e.g. closed days)."""
    date = pl.col("fecha").str.to_date() if selected.schema["fecha"] == pl.String else pl.col("fecha").cast(pl.Date)
    data = (selected.select(date, "demanda_predicha").sort("fecha")
            .with_columns((pl.col("fecha").diff().dt.total_days().fill_null(1) > 1).cum_sum().alias("tramo")))
    base = alt.Chart(data.to_pandas()).encode(
        x=alt.X("fecha:T", title="Fecha"), y=alt.Y("demanda_predicha:Q", title="Demanda predicha"),
        tooltip=[alt.Tooltip("fecha:T", title="Fecha", format="%a %d-%m-%Y"),
                 alt.Tooltip("demanda_predicha:Q", title="Demanda predicha", format=",.1f")])
    return base.mark_line(color="#D1383A").encode(detail="tramo:N") + base.mark_point(color="#D1383A", filled=True)


def order_graphs(graphs: list[Path], products: list[str]) -> list[Path]:
    """Follow the product list order; R names each file NN_<product with non [A-Za-z0-9_-] as _>.png."""
    position = {re.sub(r"[^A-Za-z0-9_-]", "_", product)[:65]: index for index, product in enumerate(products)}
    return sorted(graphs, key=lambda path: (position.get(path.stem.split("_", 1)[-1], len(position)), path.name))


def show_inventory(output: Path) -> None:
    workbook = output / "modelo_inventario.xlsx"
    if workbook.exists():
        st.download_button("Descargar Excel de inventario", workbook.read_bytes(), file_name=workbook.name,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           key="download-inventory")
    weekly_path, policy_path = output / "resumen_semanal.parquet", output / "politica_ingrediente.parquet"
    if not weekly_path.exists():
        st.info("Descarga el Excel para consultar el inventario por ingrediente.")
        return
    weekly = pl.read_parquet(weekly_path)
    if weekly.is_empty():
        st.warning("Ningún producto pronosticado calza con la hoja «composicion» del Excel de parámetros.")
        return
    confidence = float(weekly["nivel_confianza"][0])
    a, b = st.columns(2)
    site = a.selectbox("Local", weekly["local"].unique(maintain_order=True).to_list(), key="inventory-site",
                       format_func=lambda code: SITE_LABELS.get(code, code))
    site_rows = weekly.filter(pl.col("local") == site)
    with b:
        ingredients = ordered_options(site_rows, "ingrediente", "porciones_esperadas", "inventory-ingredient")
        ingredient = st.selectbox("Ingrediente o preparación", ingredients, key="inventory-ingredient",
                                  help="Ordenados por porciones esperadas en el horizonte, salvo que actives el orden alfabético.")
    selected = site_rows.filter(pl.col("ingrediente") == ingredient).sort("semana_inicio")
    first = selected.row(0, named=True)
    c1, c2, c3 = st.columns(3)
    c1.metric("Porciones esperadas (1.ª semana)", f"{first['porciones_esperadas']:,.0f}",
              help="Porciones del ingrediente: platos pronosticados × cantidad_por_plato.")
    c2.metric(f"IC {confidence:.0%}", f"{first['ic_inferior']:,.0f} – {first['ic_superior']:,.0f}")
    c3.metric("Vida útil (días)", "Sin dato" if first["vida_util_dias"] is None else f"{first['vida_util_dias']:g}")
    st.caption(f"Platos que lo usan: {first['platos_que_lo_usan']}. La semana parcial muestra solo los días del "
               "horizonte. El intervalo supone errores normales e independientes entre días y platos.")
    st.dataframe(selected.select("semana_inicio", "dias_en_horizonte", "porciones_esperadas", "desviacion_estandar",
                                 "ic_inferior", "ic_superior", *(["porciones_a_preparar"]
                                 if "porciones_a_preparar" in selected.columns else [])),
                 hide_index=True, width="stretch")
    if policy_path.exists():
        policy = pl.read_parquet(policy_path).filter((pl.col("local") == site) & (pl.col("ingrediente") == ingredient))
        with st.expander("Preparaciones sugeridas por entrega"):
            st.dataframe(policy.select("fecha_entrega", "dia_entrega", "dias_cubiertos",
                                       "demanda_esperada_ciclo", "stock_objetivo", "pedido_sugerido",
                                       "destino_sobrante", "restriccion_vida_util", "fill_rate_esperado"),
                         hide_index=True, width="stretch")
    with st.expander("Todos los ingredientes de la semana"):
        week = st.selectbox("Semana", site_rows["semana_inicio"].unique().sort().to_list(), key="inventory-week")
        st.dataframe(site_rows.filter(pl.col("semana_inicio") == week)
                     .select("ingrediente", "porciones_esperadas", "ic_inferior", "ic_superior", "vida_util_dias",
                             "platos_que_lo_usan"), hide_index=True, width="stretch")
    missing = output / "productos_sin_composicion.parquet"
    if missing.exists() and not (excluded := pl.read_parquet(missing)).is_empty():
        with st.expander(f"Productos sin composición ({excluded.height}): no entran al inventario"):
            st.dataframe(excluded.sort("demanda_horizonte_excluida", descending=True), hide_index=True, width="stretch")
    stale = output / "productos_sin_ventas_recientes.parquet"
    if stale.exists() and not (gone := pl.read_parquet(stale)).is_empty():
        with st.expander(f"Productos sin ventas recientes ({gone.height}): no entran al inventario"):
            st.caption("No se vendieron en los últimos días de datos (salieron de la carta o cambiaron de nombre), "
                       "aunque el modelo todavía les asigne un pronóstico. El reporte de producción aplica la misma regla.")
            st.dataframe(gone.sort("demanda_horizonte_excluida", descending=True), hide_index=True, width="stretch")


def show_report(run: Path) -> None:
    """Production report (PDF) for the central kitchen, or why it was not generated."""
    directory = run / "out" / REPORT_OUTPUT_SUBDIRECTORY
    status = read_status(directory)
    if status is None:
        return
    pdf = directory / (status.get("archivo") or "")
    if status.get("estado") == "generado" and pdf.is_file():
        with st.container(border=True, key="report-card"):
            st.subheader("Reporte de producción")
            st.caption(f"{status['mensaje']} Cuánto producir por ciclo y cuánto despachar a cada local, "
                       "para la cocina central.")
            st.download_button("Descargar reporte de producción (PDF)", pdf.read_bytes(), file_name=pdf.name,
                               mime="application/pdf", type="primary", key="download-report")
    elif status.get("estado") == "generado":
        st.warning("El reporte de producción se generó, pero el archivo ya no está en la carpeta de la ejecución. "
                   "Vuelve a ejecutar el pronóstico para recuperarlo.")
    elif status.get("estado") == "omitido":
        st.info(status["mensaje"])
    else:
        st.warning(f"No se generó el reporte de producción. {status.get('mensaje', '')}")


def show_results(run: Path, sites: list[str]) -> None:
    section(3, "Tus resultados")
    st.success("El pronóstico está listo. Puedes revisar las tablas y descargar los informes.")
    st.caption(f"Ejecución: {run.name}. Los resultados corresponden a los parámetros guardados en esta ejecución.")
    st.download_button("Descargar todos los resultados (.zip)", result_zip(run),
                       file_name=f"mirai-{run.name}.zip", mime="application/zip", type="primary")
    show_report(run)
    inventory_output = run / "out" / INVENTORY_SETTINGS.output_subdirectory
    has_inventory = inventory_output.exists()
    names = [SITE_LABELS[site] for site in sites]
    tabs = st.tabs([*names, "INVENTARIO"] if has_inventory else names)
    if has_inventory:
        with tabs[-1]:
            show_inventory(inventory_output)
    for tab, site in zip(tabs, sites):
        with tab:
            output = run / "out" / MODEL_SETTINGS[site].output_subdirectory
            snapshot = json.loads((output / "config_used.json").read_text(encoding="utf-8"))
            st.caption(f"Horizonte: {snapshot['horizon_days']} días calendario · Modelo: {MODELS[snapshot['model']]}")
            workbook = output / f"modelo_demanda_{WORKBOOK_NAMES[site]}.xlsx"
            if workbook.exists():
                st.download_button(f"Descargar Excel de {SITE_LABELS[site]}", workbook.read_bytes(), file_name=workbook.name,
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=f"download-{site}")
            forecast = output / "pronosticos.parquet"
            products = []
            if forecast.exists():
                frame = pl.read_parquet(forecast)
                c1, c2, c3 = st.columns(3)
                c1.metric("Productos", frame["producto"].n_unique())
                c2.metric("Fechas proyectadas", frame["fecha"].n_unique())
                c3.metric("Unidades proyectadas", f"{frame['demanda_predicha'].sum():,.0f}")
                products = ordered_options(frame, "producto", "demanda_predicha", f"product-{site}")
                product = st.selectbox("Ver producto", products, key=f"product-{site}",
                                       help="Ordenados por unidades proyectadas, salvo que actives el orden alfabético.")
                selected = frame.filter(pl.col("producto") == product).sort("fecha")
                st.altair_chart(forecast_chart(selected), width="stretch")
                st.dataframe(selected, hide_index=True, width="stretch")
            else:
                st.info("Descarga el Excel para consultar los pronósticos.")
            graphs = order_graphs(sorted((output / "graphs").glob("*.png")), products)
            if graphs:
                with st.expander("Explorar gráficos de diagnóstico"):
                    graph = st.selectbox("Gráfico", graphs, format_func=lambda p: p.stem, key=f"graph-{site}")
                    st.image(str(graph))
    st.caption(f"Copia local: {run}")
    st.info("El modelo proyecta ventas observadas. Revisa los productos de temporada y los quiebres de stock antes de planificar producción.")


with st.sidebar:
    st.title("MIRAI")
    st.caption("Del Excel a tu próxima semana")
    st.markdown("**Cómo funciona**\n\n1. Carga las ventas de cada local.\n2. Define cuántos días proyectar y cuándo se despacha.\n3. Ejecuta y descarga el informe.")
    st.divider()
    try:
        runtime_ready = find_rscript(REPOSITORY_ROOT) is not None
        if runtime_ready:
            st.success("Motor R encontrado")
            st.caption("Los paquetes se verifican al ejecutar.")
        else:
            st.warning("Falta preparar el motor R")
    except PipelineError as exc:
        runtime_ready = False
        st.warning(str(exc))
    st.caption("Abre iniciar_interfaz.bat para preparar el entorno y abrir esta pantalla.")
    st.caption("Los archivos y resultados se guardan en este equipo y están excluidos de Git.")
    st.divider()
    view = st.radio("Vista", ["Pronóstico", "Valores por defecto"], key="view",
                    help="En «Valores por defecto» se cambian los valores iniciales de esta pantalla y el Excel base "
                         "de composición de platos y vida útil.")

try:
    DEFAULTS = load_user_defaults(REPOSITORY_ROOT)
    DEFAULTS_ERROR = None
except ValueError as exc:
    DEFAULTS, DEFAULTS_ERROR = code_defaults(), str(exc)

FORECAST_WIDGET_PREFIXES = ("days-", "model-", "delivery-", "shortage-", "transfer-")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def forget_forecast_widgets() -> None:
    """Let the forecast view start again from the new defaults."""
    for key in list(st.session_state):
        if isinstance(key, str) and key.startswith(FORECAST_WIDGET_PREFIXES):
            del st.session_state[key]


def show_defaults_view() -> None:
    st.markdown("""
<div class="mirai-hero">
  <div class="mirai-kicker">Mirai Food Lab / Configuración</div>
  <h1>Valores por defecto</h1>
  <p>Define con qué valores parte cada análisis y actualiza el Excel base de composición de platos y vida útil.</p>
</div>""", unsafe_allow_html=True)
    if DEFAULTS_ERROR:
        st.error(f"Los valores guardados no son válidos y se usan los del código: {DEFAULTS_ERROR}")
    section(1, "Parámetros iniciales", "Se usan al abrir la pantalla de pronóstico y al ejecutar main.bat.")
    current = DEFAULTS
    first = next(iter(current.model_settings.values()))
    with st.form("defaults-form"):
        st.subheader("Pronóstico")
        a, b = st.columns(2)
        horizon = a.number_input("Días calendario a proyectar", min_value=1, max_value=365, value=first.horizon_days,
                                 key="def-horizon")
        history = b.number_input("Días históricos visibles en los gráficos", min_value=1, max_value=730,
                                 value=first.history_plot_days, key="def-history")
        c, d, e = st.columns(3)
        minimum = c.number_input("Mínimo de observaciones para variabilidad por día", min_value=2, max_value=100,
                                 value=first.residual_min_observations, key="def-minimum")
        plots = d.checkbox("Generar gráficos por producto", value=first.generate_plots, key="def-plots")
        parquet = e.checkbox("Incluir tablas Parquet", value=first.export_parquet, key="def-parquet")
        model_settings, inventory_sites = {}, {}
        for site in MODEL_SETTINGS:
            model, stock = current.model_settings[site], current.inventory_sites[site]
            st.subheader(SITE_LABELS[site])
            f, g = st.columns(2)
            opening = f.multiselect("Días de apertura", list(WEEKDAYS), default=list(model.operating_weekdays),
                                    format_func=WEEKDAYS.get, key=f"def-days-{site}")
            pattern = g.selectbox("Patrón de ventas", list(MODELS), index=list(MODELS).index(model.model),
                                  format_func=MODELS.get, key=f"def-model-{site}")
            h, j = st.columns(2)
            deliveries = h.multiselect("Días de despacho de pedidos", list(WEEKDAYS), default=list(stock.delivery_weekdays),
                                       format_func=WEEKDAYS.get, key=f"def-delivery-{site}", help=DISPATCH_HELP)
            shortage = j.number_input("Costo de quiebre (veces el costo de una porción)", min_value=0.1, max_value=100.0,
                                      value=float(stock.shortage_cost), step=0.5, key=f"def-shortage-{site}")
            transfer = st.checkbox("Enviar sobrantes vigentes a otro local al día siguiente", value=stock.transfer_leftovers,
                                   key=f"def-transfer-{site}")
            model_settings[site] = replace(model, horizon_days=int(horizon), history_plot_days=int(history),
                                           residual_min_observations=int(minimum), generate_plots=plots,
                                           export_parquet=parquet, operating_weekdays=tuple(opening), model=pattern)
            inventory_sites[site] = replace(stock, delivery_weekdays=tuple(deliveries), shortage_cost=float(shortage),
                                            transfer_leftovers=transfer)
        st.subheader("Inventario por ingrediente")
        k, m, n = st.columns(3)
        enabled = k.checkbox("Calcular inventario por ingrediente", value=current.inventory.enabled, key="def-inventory")
        confidence = m.slider("Nivel de confianza del intervalo semanal", min_value=0.80, max_value=0.99,
                              value=float(current.inventory.confidence_level), step=0.01, format="%.2f",
                              key="def-confidence")
        service = n.slider("Nivel de servicio sin vida útil conocida", min_value=0.50, max_value=0.99,
                           value=float(current.inventory.default_service_level), step=0.01, format="%.2f",
                           key="def-service")
        submitted = st.form_submit_button("Guardar valores por defecto", type="primary")
    if submitted:
        candidate = Defaults(model_settings, inventory_sites,
                             replace(current.inventory, enabled=enabled, confidence_level=float(confidence),
                                     default_service_level=float(service)))
        try:
            path = save_user_defaults(candidate, REPOSITORY_ROOT)
        except ValueError as exc:
            st.error(f"No se guardaron los cambios: {exc}. Revisa que cada local tenga días de apertura y de despacho.")
        else:
            forget_forecast_widgets()
            st.session_state["defaults_message"] = f"Valores guardados en {path.relative_to(REPOSITORY_ROOT).as_posix()}."
            st.rerun()
    if message := st.session_state.pop("defaults_message", None):
        st.success(message)
    saved = user_defaults_path(REPOSITORY_ROOT)
    if saved.is_file():
        st.caption(f"Valores guardados en {saved.relative_to(REPOSITORY_ROOT).as_posix()}.")
        if st.button("Restaurar valores originales", key="reset-defaults"):
            reset_user_defaults(REPOSITORY_ROOT)
            forget_forecast_widgets()
            st.session_state["defaults_message"] = "Se restauraron los valores originales del código."
            st.rerun()
    else:
        st.caption("Se están usando los valores originales del código (settings/config.py).")

    section(2, "Composición de platos y vida útil",
            "Excel base con las hojas «vida_util» y «composicion». Cada análisis usa una copia de este archivo.")
    try:
        base = default_parameters_workbook(REPOSITORY_ROOT)
        base_rows = read_parameters_workbook(base)
    except (OSError, ValueError) as exc:
        base, base_rows = None, None
        st.error(f"El Excel base no se pudo leer: {exc}. Sube uno válido para reemplazarlo.")
    if base_rows is not None:
        st.download_button("Descargar Excel base", base, file_name="inventario_parametros.xlsx", mime=XLSX_MIME,
                           key="download-base-parameters")
        for tab, (sheet, columns) in zip(st.tabs([f"{name} ({len(base_rows[name])} filas)"
                                                  for name in INVENTORY_PARAMETER_SHEETS]),
                                         INVENTORY_PARAMETER_SHEETS.items()):
            with tab:
                st.dataframe(pl.DataFrame([[None if v is None else str(v) for v in row] for row in base_rows[sheet]],
                                          schema=list(columns), orient="row"), hide_index=True, width="stretch")
    upload_key = f"upload-base-parameters-{st.session_state.get('base_upload_round', 0)}"
    upload = st.file_uploader("Nuevo Excel base", type=["xlsx"], key=upload_key,
                              help="Debe tener las hojas «vida_util» y «composicion» con sus columnas. "
                                   "Se compara con el Excel base antes de reemplazarlo.")
    if message := st.session_state.pop("base_message", None):
        st.success(message)
    if upload is None:
        return
    content = upload.getvalue()
    try:
        summary = (compare_parameters_workbooks(base, content) if base_rows is not None
                   else [{"hoja": sheet, "filas_actuales": 0, "filas_nuevas": len(rows), "añadidas": len(rows),
                          "eliminadas": 0, "sin_cambios": 0} for sheet, rows in read_parameters_workbook(content).items()])
    except ValueError as exc:
        st.error(f"Archivo rechazado: {exc}")
        st.caption("El Excel base no se modificó. Corrige el archivo y vuelve a subirlo.")
        return
    st.markdown("**Cambios respecto del Excel base**")
    for column, row in zip(st.columns(len(summary)), summary):
        column.metric(f"{row['hoja']}: añadidas / eliminadas", f"+{row['añadidas']} / −{row['eliminadas']}")
    st.dataframe(pl.DataFrame(summary), hide_index=True, width="stretch")
    st.caption("Una fila modificada cuenta como una eliminada y una añadida.")
    if not any(row["añadidas"] or row["eliminadas"] for row in summary):
        st.info("El archivo no tiene cambios respecto del Excel base.")
        return
    if st.button("Reemplazar Excel base", type="primary", key="replace-base-parameters"):
        backup = replace_default_parameters(content, REPOSITORY_ROOT)
        changes = "; ".join(f"{row['hoja']}: +{row['añadidas']} / −{row['eliminadas']}" for row in summary)
        st.session_state["base_message"] = (f"Excel base reemplazado ({changes}). Respaldo del anterior: "
                                            f"{backup.relative_to(REPOSITORY_ROOT).as_posix()}.")
        st.session_state["base_upload_round"] = st.session_state.get("base_upload_round", 0) + 1
        st.rerun()


if view == "Valores por defecto":
    show_defaults_view()
    st.stop()
if DEFAULTS_ERROR:
    st.warning(f"Los valores por defecto guardados no son válidos; se usan los del código. {DEFAULTS_ERROR}")

st.markdown("""
<div class="mirai-bar">Pronóstico de ventas por producto para MUT, Franklin y la tienda
  <a href="#carga">Comenzar ahora&nbsp;→</a></div>
<div class="mirai-hero">
  <div class="mirai-kicker">Mirai Food Lab / Planificación</div>
  <h1>Tus ventas, un paso adelante</h1>
  <p>Carga los Excel de tus locales y prepara un pronóstico de ventas por producto, sin escribir código.</p>
  <a class="mirai-link" href="#carga">Carga tus ventas</a>
</div>
""", unsafe_allow_html=True)

MINIMUM_WEEKS = next(iter(DEFAULTS.model_settings.values())).residual_min_observations
st.markdown(f"""
<div class="mirai-history" role="alert">
  <div class="icon">⚠️</div>
  <div>
    <h2>Tener ventas de al menos un año atrás enriquece enormemente el pronóstico</h2>
    <ul>
      <li>Carga <b>todos los meses que tengas de cada local, desde hace al menos un año</b>: así el modelo
          aprende cómo cambia la demanda de un mes a otro y entre temporadas. Puedes subir varios Excel a la vez.</li>
      <li>Con poca historia el modelo no alcanza a aprender los patrones por día de la semana y por mes,
          y usa un modelo más simple y menos preciso.</li>
      <li>Para medir la variabilidad de cada día de la semana se necesitan al menos {MINIMUM_WEEKS} semanas
          de ventas; con menos, el margen del reporte de producción es menos exacto.</li>
    </ul>
  </div>
</div>
""", unsafe_allow_html=True)

section(1, "Carga tus ventas", anchor="carga")
sites = st.multiselect("¿Qué locales quieres analizar?", list(MODEL_SETTINGS), default=["MUT"], format_func=SITE_LABELS.get,
                       help="Puedes ejecutar uno o varios locales. Cada local tendrá su propio informe.")
uploads = {}
for site in sites:
    with st.container(border=True, key=f"site-card-{site}"):
        st.subheader(SITE_LABELS[site])
        if site == "MUT":
            st.caption("Exportación de pedidos de MUT · hoja Sheet1. Se consideran los pedidos pagados.")
        elif site == "FRANKLIN":
            st.caption("Exportación de ventas por producto de Franklin · hoja Detalle.")
            st.info("El reporte de la tienda (hoja «Detalle de ventas») va en Tienda Franklin, no aquí.")
        else:
            st.caption("Reporte de la tienda Franklin · hoja «Detalle de ventas». Se consideran las ventas; "
                       "las notas de crédito (devoluciones y anulaciones) no cuentan como demanda.")
        files = st.file_uploader(f"Excel de {SITE_LABELS[site]}", type=["xlsx", "xlsm"], accept_multiple_files=True, key=f"upload-{site}",
                                help="Arrastra uno o varios archivos. Usa períodos que no se superpongan.")
        uploads[site] = [(f.name, f.getvalue()) for f in files]
        if files:
            st.caption(f"{len(files)} archivo(s) · {sum(f.size for f in files) / 1024 / 1024:.1f} MB")

section(2, "Ajusta tu pronóstico")
left, right = st.columns([1, 2])
with left:
    horizon = st.number_input("Días calendario a proyectar", min_value=1, max_value=365,
                              value=next(iter(DEFAULTS.model_settings.values())).horizon_days,
                              help="Se cuentan desde la última fecha de ventas de cada local. Solo se proyectan sus días de apertura.")
with right:
    st.info("Puedes dejar los valores sugeridos para comenzar. Los locales se analizan por separado, aunque los ejecutes juntos.")
settings = {}
inventory_sites = {}
for site in sites:
    default = DEFAULTS.model_settings[site]
    with st.expander(f"Configuración de {SITE_LABELS[site]}", expanded=True):
        a, b = st.columns(2)
        with a:
            weekdays = st.multiselect("Días de apertura", list(WEEKDAYS), default=list(default.operating_weekdays),
                                       format_func=WEEKDAYS.get, key=f"days-{site}")
        with b:
            model = st.selectbox("Patrón de ventas", list(MODELS), index=list(MODELS).index(default.model),
                                 format_func=MODELS.get, key=f"model-{site}",
                                 help="El patrón por mes necesita más historia. Con pocos datos el motor usa un modelo más simple.")
        settings[site] = replace(default, horizon_days=int(horizon), operating_weekdays=tuple(weekdays), model=model)
        stock = DEFAULTS.inventory_sites[site]
        c, e = st.columns(2)
        with c:
            deliveries = st.multiselect("Días de despacho de pedidos", list(WEEKDAYS), default=list(stock.delivery_weekdays),
                                        format_func=WEEKDAYS.get, key=f"delivery-{site}", help=DISPATCH_HELP)
        with e:
            shortage = st.number_input("Costo de quiebre (veces el costo de una porción)", min_value=0.1, max_value=100.0,
                                       value=float(stock.shortage_cost), step=0.5, key=f"shortage-{site}",
                                       help="Supuesto a validar: cuánto peor es que falte una porción que botar una.")
        transfer = st.checkbox("Enviar sobrantes vigentes a otro local al día siguiente", value=stock.transfer_leftovers,
                               key=f"transfer-{site}", help="Práctica descrita para Franklin: lo que sobra el domingo va al MUT.")
        inventory_sites[site] = replace(stock, delivery_weekdays=tuple(deliveries), shortage_cost=float(shortage),
                                        transfer_leftovers=transfer)

with st.expander("Inventario por ingrediente", expanded=True):
    inventory_enabled = st.checkbox("Calcular inventario por ingrediente", value=DEFAULTS.inventory.enabled,
                                    help="Usa el pronóstico de cada plato, su composición y la vida útil de cada ingrediente.")
    f, g = st.columns(2)
    with f:
        confidence = st.slider("Nivel de confianza del intervalo semanal", min_value=0.80, max_value=0.99,
                               value=DEFAULTS.inventory.confidence_level, step=0.01, format="%.2f")
    with g:
        service = st.slider("Nivel de servicio sin vida útil conocida", min_value=0.50, max_value=0.99,
                            value=DEFAULTS.inventory.default_service_level, step=0.01, format="%.2f",
                            help="Se aplica a ingredientes sin vida útil en la hoja «vida_util».")
    st.caption("La vida útil (hoja «vida_util») y los ingredientes de cada plato (hoja «composicion») están en un Excel "
               "editable. Descárgalo, modifícalo y súbelo para usarlo solo en esta ejecución. Para cambiar el Excel base "
               "usa la vista «Valores por defecto».")
    try:
        st.download_button("Descargar Excel de parámetros actual", default_parameters_workbook(REPOSITORY_ROOT),
                           file_name="inventario_parametros.xlsx", key="download-parameters",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except OSError:
        st.warning("No se encontró settings/inventario_parametros.xlsx en el repositorio.")
    custom = st.file_uploader("Excel de parámetros modificado (opcional)", type=["xlsx"], key="upload-parameters")
    parameters = custom.getvalue() if custom else None
    parameters_error = None
    if parameters is not None:
        try:
            validate_parameters_workbook(parameters)
        except ValueError as exc:
            parameters_error = str(exc)
            st.error(parameters_error)
        else:
            st.success("Se usará el Excel de parámetros cargado.")
inventory_settings = replace(DEFAULTS.inventory, enabled=inventory_enabled, confidence_level=float(confidence),
                             default_service_level=float(service))

with st.expander("Opciones avanzadas"):
    base_settings = next(iter(DEFAULTS.model_settings.values()))
    plots = st.checkbox("Generar gráficos por producto", value=base_settings.generate_plots,
                        help="Puede tardar más si hay muchos productos.")
    parquet = st.checkbox("Incluir tablas Parquet", value=base_settings.export_parquet,
                          help="Permite explorar las tablas aquí y utilizarlas en otros análisis.")
    history = st.number_input("Días históricos visibles en los gráficos", min_value=1, max_value=730,
                              value=base_settings.history_plot_days)
    minimum = st.number_input("Mínimo de observaciones para variabilidad por día", min_value=2, max_value=100,
                              value=base_settings.residual_min_observations)
    st.caption("Siempre se genera Excel. Los períodos superpuestos se rechazan para evitar contar ventas dos veces.")
    report_start_today = st.checkbox(
        "Reporte de producción: comenzar hoy", value=False, key="report-start-today",
        help="Por defecto el reporte cubre la próxima semana de producción, de viernes a jueves. "
             "Actívalo para que cubra desde hoy y los 6 días siguientes.")
settings = {site: replace(value, generate_plots=plots, export_parquet=parquet,
                          history_plot_days=int(history), residual_min_observations=int(minimum))
            for site, value in settings.items()}
inventory_settings = replace(inventory_settings, export_parquet=parquet)

ready = (bool(sites) and all(uploads.values()) and all(s.operating_weekdays for s in settings.values())
         and (not inventory_enabled or (all(s.delivery_weekdays for s in inventory_sites.values())
                                        and parameters_error is None)))
st.write("")
if not ready:
    st.caption("Para continuar, selecciona al menos un local, carga sus Excel y elige sus días de apertura y de despacho.")
st.caption("Se usarán solo los archivos de esta carga. Las ejecuciones anteriores se conservan por separado.")
if sites and set(REPORT_SITES) - set(sites):
    st.caption("El reporte de producción para la cocina central se genera cuando analizas MUT, Franklin y "
               "Tienda Franklin juntos.")
if st.button("Generar pronóstico", type="primary", disabled=not ready or not runtime_ready, width="stretch", key="generate"):
    st.session_state.pop("completed_run", None)
    st.session_state.pop("failed_run", None)
    run = None
    with st.status("Preparando el análisis…", expanded=True) as status:
        try:
            validate_uploads(uploads, settings)
            run = create_run(REPOSITORY_ROOT, uploads, settings, parameters if inventory_enabled else None)
            st.write("Archivos guardados. Validando datos y ejecutando los modelos…")
            execute_run(REPOSITORY_ROOT, run, settings, LiveLog(st.empty()),
                        inventory=inventory_settings, inventory_sites=inventory_sites,
                        report_start_today=report_start_today)
        except Exception as exc:
            status.update(label="No se pudo completar el análisis", state="error")
            st.error(str(exc))
            st.info("Revisa que los Excel tengan el formato indicado, contengan ventas válidas y no repitan períodos. El detalle técnico permite identificar el archivo o la dependencia que falló.")
            if run:
                st.session_state.failed_run = str(run)
        else:
            status.update(label="Análisis completado", state="complete", expanded=False)
            st.session_state.completed_run = (str(run), list(settings))

if "failed_run" in st.session_state:
    show_report(Path(st.session_state.failed_run))   # the PDF may exist even if a later step failed
    log = Path(st.session_state.failed_run) / "pipeline.log"
    if log.exists():
        with st.expander("Detalle de la ejecución fallida"):
            st.code(log.read_text(encoding="utf-8")[-25000:], language=None)
            st.download_button("Descargar registro de ejecución", log.read_bytes(), file_name="mirai-diagnostico.log")
if "completed_run" in st.session_state:
    path, completed_sites = st.session_state.completed_run
    show_results(Path(path), completed_sites)

st.markdown('<div class="mirai-footer"><strong>Mirai Food Lab · Planificación</strong>'
            'Herramienta interna de pronóstico de ventas · MUT, Franklin y Tienda Franklin</div>', unsafe_allow_html=True)
