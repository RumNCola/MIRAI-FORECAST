# MIRAI-FORECAST
ELT of MIRAI FOOD LAB sales data in order to forecast future demand of various products, considering diverse variables such as seasonality, holidays, weeks of the month among other causes.

# Data MGMT and Privacy
In order to respect privacy and sensible data management, al files contained within Data folder must be put in the .gitignore and should never be pushed to github.

## ELT

Install dependencies with `python -m pip install -r requirements.txt` (Python 3.11+).
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
`out/franklin/` and `out/mut/`. R requires `arrow`, `digest`, `jsonlite` and
`writexl`. See [the model documentation](scripts/README_Mirai_GitHub.md).

The `main.bat` → `main.py` → ELT → R orchestration is the intended architecture;
the Python orchestrator will be implemented separately.


