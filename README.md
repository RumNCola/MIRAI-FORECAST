# MIRAI-FORECAST
ELT of MIRAI FOOD LAB sales data in order to forecast future demand of various products, considering diverse variables such as seasonality, holidays, weeks of the month among other causes.

# Data MGMT and Privacy
In order to respect privacy and sensible data management, al files contained within Data folder must be put in the .gitignore and should never be pushed to github.

## ELT

Install dependencies with `python -m pip install -r requirements.txt` (Python 3.11+).
Run `python src/old_elt.py` to process pending RAW exports into DEL Parquet files with Polars,
or call `ELT().run()` from your application's main function. Importing the module
does not run the pipeline. Processing status and quality reports are kept locally
in `elt_log.json` at the repository root. Events go to `elt.log`: warnings allow
the batch to continue, while errors stop execution and make main return 1.

See [the ELT documentation](docs/elt.md) for supported formats, integrity checks,
product-name validation, recovery behavior and tests.


