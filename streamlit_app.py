"""Run with: python -m streamlit run streamlit_app.py."""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

import polars as pl
import streamlit as st

from settings.config import MODEL_SETTINGS, REPOSITORY_ROOT
from src.interface import create_run, execute_run, result_zip, validate_uploads
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
div[class*="st-key-site-card-"] {
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

/* Footer -------------------------------------------------------------------------------------- */
.mirai-footer {
  margin-top: 4rem; padding: 2rem 0 .5rem; border-top: 1px solid var(--line); text-align: center;
  color: #334155; font-size: .85rem;
}
.mirai-footer strong {letter-spacing: .14em; text-transform: uppercase; font-size: .78rem; display: block; margin-bottom: .3rem;}
</style>""", unsafe_allow_html=True)

WEEKDAYS = {1: "Lunes", 2: "Martes", 3: "Miércoles", 4: "Jueves", 5: "Viernes", 6: "Sábado", 7: "Domingo"}
MODELS = {"dia": "Por día de la semana", "dia_mes_anual": "Por día de la semana y mes"}


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


def show_results(run: Path, sites: list[str]) -> None:
    section(3, "Tus resultados")
    st.success("El pronóstico está listo. Puedes revisar las tablas y descargar los informes.")
    st.caption(f"Ejecución: {run.name}. Los resultados corresponden a los parámetros guardados en esta ejecución.")
    st.download_button("Descargar todos los resultados (.zip)", result_zip(run),
                       file_name=f"mirai-{run.name}.zip", mime="application/zip", type="primary")
    for tab, site in zip(st.tabs(sites), sites):
        with tab:
            output = run / "out" / MODEL_SETTINGS[site].output_subdirectory
            snapshot = json.loads((output / "config_used.json").read_text(encoding="utf-8"))
            st.caption(f"Horizonte: {snapshot['horizon_days']} días calendario · Modelo: {MODELS[snapshot['model']]}")
            workbook = output / ("modelo_demanda_Franklin.xlsx" if site == "FRANKLIN" else "modelo_demanda_MUT.xlsx")
            if workbook.exists():
                st.download_button(f"Descargar Excel de {site}", workbook.read_bytes(), file_name=workbook.name,
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=f"download-{site}")
            forecast = output / "pronosticos.parquet"
            if forecast.exists():
                frame = pl.read_parquet(forecast)
                c1, c2, c3 = st.columns(3)
                c1.metric("Productos", frame["producto"].n_unique())
                c2.metric("Fechas proyectadas", frame["fecha"].n_unique())
                c3.metric("Unidades proyectadas", f"{frame['demanda_predicha'].sum():,.0f}")
                products = sorted(frame["producto"].unique().to_list())
                product = st.selectbox("Ver producto", products, key=f"product-{site}")
                selected = frame.filter(pl.col("producto") == product).sort("fecha")
                st.line_chart(selected.select("fecha", "demanda_predicha").to_pandas(), x="fecha", y="demanda_predicha",
                              color="#D1383A")
                st.dataframe(selected, hide_index=True, width="stretch")
            else:
                st.info("Descarga el Excel para consultar los pronósticos.")
            graphs = sorted((output / "graphs").glob("*.png"))
            if graphs:
                with st.expander("Explorar gráficos de diagnóstico"):
                    graph = st.selectbox("Gráfico", graphs, format_func=lambda p: p.stem, key=f"graph-{site}")
                    st.image(str(graph))
    st.caption(f"Copia local: {run}")
    st.info("El modelo proyecta ventas observadas. Revisa los productos de temporada y los quiebres de stock antes de planificar producción.")


with st.sidebar:
    st.title("MIRAI")
    st.caption("Del Excel a tu próxima semana")
    st.markdown("**Cómo funciona**\n\n1. Carga las ventas de cada local.\n2. Define cuántos días proyectar.\n3. Ejecuta y descarga el informe.")
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

st.markdown("""
<div class="mirai-bar">Pronóstico de ventas por producto para MUT y Franklin
  <a href="#carga">Comenzar ahora&nbsp;→</a></div>
<div class="mirai-hero">
  <div class="mirai-kicker">Mirai Food Lab / Planificación</div>
  <h1>Tus ventas, un paso adelante</h1>
  <p>Carga los Excel de tus locales y prepara un pronóstico de ventas por producto, sin escribir código.</p>
  <a class="mirai-link" href="#carga">Carga tus ventas</a>
</div>
""", unsafe_allow_html=True)

section(1, "Carga tus ventas", anchor="carga")
sites = st.multiselect("¿Qué locales quieres analizar?", ["MUT", "FRANKLIN"], default=["MUT"],
                       help="Puedes ejecutar un solo local o ambos. Cada local tendrá su propio informe.")
uploads = {}
for site in sites:
    with st.container(border=True, key=f"site-card-{site}"):
        st.subheader(site)
        if site == "MUT":
            st.caption("Exportación de pedidos de MUT · hoja Sheet1. Se consideran los pedidos pagados.")
        else:
            st.caption("Exportación de ventas por producto de Franklin · hoja Detalle.")
            st.info("El reporte de tienda con hoja «Detalle de ventas» contiene movimientos y notas de crédito; el modelo actual requiere el reporte de ventas con hoja «Detalle».")
        files = st.file_uploader(f"Excel de {site}", type=["xlsx", "xlsm"], accept_multiple_files=True, key=f"upload-{site}",
                                help="Arrastra uno o varios archivos. Usa períodos que no se superpongan.")
        uploads[site] = [(f.name, f.getvalue()) for f in files]
        if files:
            st.caption(f"{len(files)} archivo(s) · {sum(f.size for f in files) / 1024 / 1024:.1f} MB")

section(2, "Ajusta tu pronóstico")
left, right = st.columns([1, 2])
with left:
    horizon = st.number_input("Días calendario a proyectar", min_value=1, max_value=365, value=28,
                              help="Se cuentan desde la última fecha de ventas de cada local. Solo se proyectan sus días de apertura.")
with right:
    st.info("Puedes dejar los valores sugeridos para comenzar. Los locales se analizan por separado, aunque los ejecutes juntos.")
settings = {}
for site in sites:
    default = MODEL_SETTINGS[site]
    with st.expander(f"Configuración de {site}", expanded=True):
        a, b = st.columns(2)
        with a:
            weekdays = st.multiselect("Días de apertura", list(WEEKDAYS), default=list(default.operating_weekdays),
                                       format_func=WEEKDAYS.get, key=f"days-{site}")
        with b:
            model = st.selectbox("Patrón de ventas", list(MODELS), index=list(MODELS).index(default.model),
                                 format_func=MODELS.get, key=f"model-{site}",
                                 help="El patrón por mes necesita más historia. Con pocos datos el motor usa un modelo más simple.")
        settings[site] = replace(default, horizon_days=int(horizon), operating_weekdays=tuple(weekdays), model=model)

with st.expander("Opciones avanzadas"):
    plots = st.checkbox("Generar gráficos por producto", value=True, help="Puede tardar más si hay muchos productos.")
    parquet = st.checkbox("Incluir tablas Parquet", value=True, help="Permite explorar las tablas aquí y utilizarlas en otros análisis.")
    history = st.number_input("Días históricos visibles en los gráficos", min_value=1, max_value=730, value=45)
    minimum = st.number_input("Mínimo de observaciones para variabilidad por día", min_value=2, max_value=100, value=5)
    st.caption("Siempre se genera Excel. Los períodos superpuestos se rechazan para evitar contar ventas dos veces.")
settings = {site: replace(value, generate_plots=plots, export_parquet=parquet,
                          history_plot_days=int(history), residual_min_observations=int(minimum))
            for site, value in settings.items()}

ready = bool(sites) and all(uploads.values()) and all(s.operating_weekdays for s in settings.values())
st.write("")
if not ready:
    st.caption("Para continuar, selecciona al menos un local, carga sus Excel y elige sus días de apertura.")
st.caption("Se usarán solo los archivos de esta carga. Las ejecuciones anteriores se conservan por separado.")
if st.button("Generar pronóstico", type="primary", disabled=not ready or not runtime_ready, width="stretch", key="generate"):
    st.session_state.pop("completed_run", None)
    st.session_state.pop("failed_run", None)
    run = None
    with st.status("Preparando el análisis…", expanded=True) as status:
        try:
            validate_uploads(uploads, settings)
            run = create_run(REPOSITORY_ROOT, uploads, settings)
            st.write("Archivos guardados. Validando datos y ejecutando los modelos…")
            execute_run(REPOSITORY_ROOT, run, settings, LiveLog(st.empty()))
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
    log = Path(st.session_state.failed_run) / "pipeline.log"
    if log.exists():
        with st.expander("Detalle de la ejecución fallida"):
            st.code(log.read_text(encoding="utf-8")[-25000:], language=None)
            st.download_button("Descargar registro de ejecución", log.read_bytes(), file_name="mirai-diagnostico.log")
if "completed_run" in st.session_state:
    path, completed_sites = st.session_state.completed_run
    show_results(Path(path), completed_sites)

st.markdown('<div class="mirai-footer"><strong>Mirai Food Lab · Planificación</strong>'
            'Herramienta interna de pronóstico de ventas · MUT y Franklin</div>', unsafe_allow_html=True)
