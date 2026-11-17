# MIRAI-FORECAST
ELT of MIRAI FOOD LAB sales data in order to forecast future demand of various products, considering diverse variables such as seasonality, holidays, weeks of the month among other causes.

## Interfaz para usuarios (Windows)

Haz doble clic en **`iniciar_interfaz.bat`**. Necesitas Python 3.12 o superior;
la primera apertura necesita Internet para preparar las dependencias Python y R.
Se abrirá una aplicación local en el navegador. Mantén abierta la consola mientras
la usas; para cerrarla presiona Ctrl+C en esa consola.

1. Selecciona MUT, Franklin o ambos y arrastra uno o varios Excel por local.
   MUT requiere la hoja `Sheet1`; Franklin requiere `Detalle`, del reporte de
   ventas por producto. El reporte Franklin Store (`Detalle de ventas`) no está
   admitido por el modelo de pronóstico actual. Usa `.xlsx` o `.xlsm`, con
   períodos que no se superpongan.
2. Elige los días calendario del horizonte, los días de apertura y el patrón de
   ventas de cada local. Los valores iniciales corresponden al modelo existente.
   El horizonte empieza después de la última venta del local, no de la fecha actual.
3. Pulsa **Generar pronóstico**. Se validan todos los archivos; si alguno es
   rechazado, corrígelo antes de ejecutar nuevamente. Los registros permiten
   identificar el problema sin editar código.
4. Revisa el pronóstico por producto y descarga el Excel de cada local o el ZIP
   con todos los informes y gráficos. La vista previa requiere la opción Parquet.

Cada carga usa únicamente sus propios archivos y se guarda por separado en
`out/ejecuciones/<identificador>/`: originales, datos procesados, registros,
parámetros y resultados. No mezcla archivos con cargas previas ni cambia los
valores de `settings/config.py`. Los locales se pronostican por separado, no se
suman sus ventas. Los datos y resultados están excluidos de Git. La aplicación
escucha solo en `127.0.0.1` y no requiere una cuenta en Streamlit.

Con el entorno preparado también puedes abrirla desde una terminal:

```powershell
.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

El código de la pantalla está en `streamlit_app.py`; el manejo de cargas y
ejecuciones está en `src/interface.py`. `main.bat` conserva la ejecución habitual
por consola de ambos modelos. No ejecutes análisis simultáneos: comparten el
bloqueo del proyecto. Las métricas del modelo son de ajuste histórico y no
garantizan precisión futura.

# Data MGMT and Privacy
In order to respect privacy and sensible data management, al files contained within Data folder must be put in the .gitignore and should never be pushed to github.

## Run the complete pipeline on Windows

With Python 3.12 or newer installed, run or double-click `main.bat`. The first
execution needs Internet access to install dependencies. It:

1. Creates/reuses `.venv` and installs `requirements.txt` when needed.
2. Finds an existing R installation or downloads the configured official CRAN
   installer, verifies its published checksum and installs R under `.runtime/`.
3. Installs missing R packages in a project library separated by R version.
4. Calls `main.py`: incremental ELT → Franklin model → MUT model.

Later executions reuse the environments and processed DEL artifacts. The launcher
works from any current directory and stops when setup fails. Runtime settings,
the CRAN mirror and model-script paths live in `settings/config.py`.

If the environment is already prepared, run `.venv\Scripts\python.exe main.py`
directly. This entry point validates configuration and R dependencies but does
not install software. To use a particular R installation, set `MIRAI_RSCRIPT` to
its `Rscript.exe` path before execution. Both entry points return exit code 1
for fatal failures; recoverable ELT warnings allow the accepted data to continue.

`pipeline.log` records setup, ELT and model progress, warnings, child-process
output and fatal tracebacks. Success messages appear after their stage completes.
`elt.log` and `elt_log.json` retain the detailed ELT events and processing state.
An exclusive `.pipeline.lock` prevents concurrent setup/model runs; remove a
stale lock only after confirming the previous process has stopped.

Private runtime downloads, installed R packages and setup fingerprints live in
the ignored `.runtime/` directory. Python dependencies are rechecked after a
change to requirements or the interpreter, or a failed dependency/import check.

## ELT

Install dependencies with `python -m pip install -r requirements.txt` (Python 3.12+).
Run `python src/elt.py` to process pending RAW exports into DEL Parquet files with Polars,
or call `ELT().run()` from your application's main function. Importing the module
does not run the pipeline. Processing status and quality reports are kept locally
in `elt_log.json` at the repository root. Events go to `elt.log`: warnings allow
the batch to continue, while errors stop execution and make main return 1.

See [the ELT documentation](docs/elt.md) for supported formats, integrity checks,
product-name validation, recovery behavior and tests.

## Demand models

Shared settings live in `settings/config.py`: horizon, model, operating weekdays,
input columns, output formats and plots. Run the ELT before the R models:

```powershell
Rscript scripts/modelo_demanda_Franklin.R
Rscript scripts/modelo_demanda_MUT.R
```

The scripts read accepted DEL Parquet files using `elt_log.json` and save their
analysis tables, Excel reports, plots and configuration snapshots in
`out/franklin/` and `out/mut/`. R requires `arrow`, `digest`, `jsonlite`, `tzdb` and
`writexl`. See [the model documentation](scripts/README_Mirai_GitHub.md).

The implemented execution order is `main.bat` → environment setup → `main.py`
→ ELT → Franklin → MUT. If either model fails, subsequent stages stop and the
pipeline does not record a successful completion.


