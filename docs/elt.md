# RAW-to-DEL processing

Use Python 3.11 or newer. Install the dependencies with `python -m pip install -r requirements.txt`.

```python
from src.old_elt import ELT

result = ELT().run()
if result.failed:
    raise RuntimeError(f"{result.failed} exports failed validation")
```

Importing `old_elt` does not read or write data. The application owns its main
function. For a standalone batch, run `python src/old_elt.py`. It processes every
pending export, continues after individual file failures, and exits with code 1
if any file fails. Paths are relative to the repository containing the module,
independent of the working directory. Tests may supply `ELT(repository_root=...)`.

## Input formats

RAW accepts `.xlsx`, `.xlsm` and `.parquet` recursively under these directories:

| Directory | Excel detail sheet | Format and cleaning method |
| --- | --- | --- |
| `Data/RAW/FRANKLIN_SALES/` | `Detalle` | Daily quantities by date/category/subcategory/product; `clean_franklin_sales`. |
| `Data/RAW/FRANKLIN_STORE/` | `Detalle de ventas` | Document product lines, including variants and negative credit notes; `clean_franklin_store`. Headers currently start on row 6. |
| `Data/RAW/MUT_SALES/` | `Sheet1` | Order product lines; `clean_mut_sales`. Both current layouts are supported, including the newer monetary columns. |
| `Data/RAW/MUT_STORE/` | `Sheet1` | Optional alias using the same format as `MUT_SALES`; `clean_mut_store`. The repository currently uses `MUT_SALES`. |

The loader finds required headers within the first 20 rows. Excel lock files
(`~$...`) and hidden files are ignored. Auxiliary ranking, summary and exclusion
sheets are never appended as sales rows. Ranking and available store summary
counts are used as reconciliation controls. Old `.xls`/`.xlsb` files must be
converted to a supported format first.

RAW Parquet may use the original Spanish labels or the translated English
columns. It must contain an export table without DEL provenance columns.

## Output and preserved values

Each export produces `Data/DEL/<source>/<relative-input-filename>.parquet` with
Snappy compression. The input extension stays in the filename to prevent Excel
and Parquet inputs with the same stem from colliding. The source directory and
any nested directories are preserved. RAW files are never overwritten.

Columns use English names such as `date`, `category`, `subcategory`,
`product_name`, `quantity`, `order_id`, `product_sku`, `movement` and `variant`.
Dates are timestamps without a timezone; quantities are signed 64-bit integers;
available monetary amounts are numeric; identifiers and other attributes are
strings. Product names, capitalization, accents, whitespace and variants are
preserved. Leading zeroes in identifiers are preserved when stored as text in
RAW; an Excel numeric cell cannot recover zeroes that were already discarded.
Populated columns without a header are retained as `source_unlabeled_<position>`.
Entirely empty rows and entirely empty unlabeled columns are removed.

Every output row includes `source`, `source_file`, `source_sheet`, `source_row`
and `source_sha256`. Excel row numbers refer to physical worksheet rows; RAW
Parquet row numbers start at 1. Each temporary Parquet is read back and compared
to the prepared table before it atomically replaces the destination.

## Integrity checks

Publication fails for missing sheets or required columns, duplicate headers,
reserved provenance columns in RAW, empty detail tables, missing required values,
non-text products/categories, invalid or ambiguous date formats, non-finite
numbers, fractional or zero unit counts, invalid MUT times/currency, or Excel
errors in detail/control tables. Dates accept native Excel dates, ISO dates,
`DD/MM/YYYY`, `DD-MM-YYYY` and `YYYY/MM/DD`; unformatted Excel serial numbers and
dates with time components are rejected.

Franklin Sales requires positive quantities and a unique
date/category/subcategory/product key. If its ranking sheet is available,
quantities must reconcile per category/subcategory/product. Franklin Store
requires positive sales, negative credit notes and consistent movement/document
types. Available summary row/document counts must reconcile. MUT quantities
must be positive; refunds require an explicit business rule before ingestion.
The export's names and per-row quantities must survive cleaning unchanged.

Warnings record repeated transaction lines, surrounding whitespace, populated
unlabeled columns, future dates, non-paid MUT lines, inconsistent order dates or
statuses, overlapping export periods, missing controls, errors in the auxiliary
store summary, and days without observations. Missing days are reported without
inventing zero sales. Repeated transaction lines are retained: the available
exports do not provide a reliable unique line identifier. Review overlapping
exports before concatenating their DEL tables; overlap is reported, not merged.

The current Franklin Store summary contains Excel formula errors. They are
reported without discarding its valid detail table. No revenue reconciliation
is inferred from that broken summary.

## Processing log and product catalog

`elt_log.json` in the repository root contains one current record per input path:
source/output SHA-256 hashes, pipeline version, processing timestamp, output path,
row counts, quantity totals, date ranges, null counts, exact product names and
warnings. Failed files record their error and retain the previous successful
record under `last_success`, when available. Last-run counts are also recorded.
The log and `Data/` are excluded from Git because they contain private metadata.

The first fully successful batch for each source saves **all its exact current
product names** as that source's `product_catalog`. It does not adopt the old
correction dictionary. Future names outside that catalog generate warnings with
possible matches; neither spelling corrections nor automatic approvals occur.
To approve a new name, review it and add its exact spelling to the corresponding
catalog list in the local log before the next batch. Existing accepted spellings
are never treated as errors merely because they resemble one another.

An export is skipped only when its content hash and pipeline version match a
successful record and its DEL file still matches the saved output hash. Changed
exports are processed again; missing or corrupted outputs are rebuilt. Exact
binary copies within one source reuse one output and retain the original
artifact's provenance. Identical rows in different nonidentical exports are not
automatically deduplicated. `MUT_STORE` and `MUT_SALES` are separate source labels;
use one directory convention consistently.

Writes to the log are atomic and happen after each file. An exclusive `.elt.lock`
prevents concurrent runs. If a process crashes, confirm it has stopped before
removing a stale lock. A corrupt log stops processing instead of resetting
history. Increment `PIPELINE_VERSION` when cleaning rules change to rebuild
existing artifacts. An invalid updated RAW file leaves the last accepted DEL
artifact in place, marked as historical through `last_success`; do not treat it
as the successfully processed version of that updated RAW file. Deleting RAW
does not automatically delete DEL or its log records.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Tests use synthetic Excel and Parquet inputs, requiring no private data.
