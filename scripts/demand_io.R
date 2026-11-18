# Shared configuration, validated DEL inputs and analysis outputs.
# This file defines helpers only; sourcing it never starts an analysis.

require_model_packages <- function(packages) {
  missing <- packages[!vapply(packages, requireNamespace, logical(1), quietly = TRUE)]
  if (length(missing)) {
    stop("Missing R packages: ", paste(missing, collapse = ", "),
         ". Install them before running the models.", call. = FALSE)
  }
}

read_model_config <- function(site, repository_root) {
  require_model_packages("jsonlite")
  override <- Sys.getenv("MIRAI_MODEL_CONFIG", unset = "")
  if (nzchar(override)) {
    config <- jsonlite::fromJSON(override, simplifyVector = TRUE)
    if (!identical(config$schema_version, 1L) || !identical(config$site, site)) {
      stop("Unexpected model configuration contract", call. = FALSE)
    }
    require_model_packages(config$required_r_packages)
    return(config)
  }
  explicit_python <- Sys.getenv("MIRAI_PYTHON", unset = "")
  if (nzchar(explicit_python)) {
    python <- if (file.exists(explicit_python)) explicit_python else Sys.which(explicit_python)
    if (!nzchar(python)) stop("MIRAI_PYTHON does not identify an executable", call. = FALSE)
  } else {
    candidates <- c(file.path(repository_root, ".venv", "Scripts", "python.exe"),
                    file.path(repository_root, ".venv", "bin", "python"),
                    unname(Sys.which(c("python", "python3"))))
    candidates <- candidates[nzchar(candidates) & file.exists(candidates)]
    if (!length(candidates)) stop("Python was not found; prepare the repository environment first", call. = FALSE)
    python <- candidates[[1]]
  }
  errors <- tempfile(fileext = ".log")
  on.exit(unlink(errors), add = TRUE)
  result <- suppressWarnings(system2(
    python,
    args = c(shQuote(file.path(repository_root, "settings", "config.py")), "--site", site),
    stdout = TRUE, stderr = errors
  ))
  status <- attr(result, "status")
  if (!is.null(status) && status != 0L) {
    stop("Could not load settings/config.py: ", paste(readLines(errors, warn = FALSE), collapse = "\n"), call. = FALSE)
  }
  config <- jsonlite::fromJSON(paste(result, collapse = "\n"), simplifyVector = TRUE)
  if (!identical(config$schema_version, 1L) || !identical(config$site, site)) {
    stop("Unexpected model configuration contract", call. = FALSE)
  }
  require_model_packages(config$required_r_packages)
  config
}

path_is_within <- function(path, directory) {
  path <- normalizePath(path, winslash = "/", mustWork = TRUE)
  directory <- paste0(normalizePath(directory, winslash = "/", mustWork = TRUE), "/")
  if (.Platform$OS.type == "windows") {
    path <- tolower(path)
    directory <- tolower(directory)
  }
  startsWith(path, directory)
}

select_del_records <- function(config) {
  if (!file.exists(config$processing_log)) {
    stop("ELT processing log is missing. Run ELT before the demand models.", call. = FALSE)
  }
  manifest <- jsonlite::fromJSON(config$processing_log, simplifyVector = FALSE)
  if (!identical(manifest$log_version, 1L) || !is.list(manifest$files)) {
    stop("Unsupported ELT processing log", call. = FALSE)
  }
  relevant <- Filter(function(entry) identical(entry$source, config$source), manifest$files)
  rejected <- names(relevant)[!vapply(relevant, function(entry) identical(entry$status, "processed"), logical(1))]
  if (length(rejected)) {
    warning("Exports rejected by ELT are excluded: ", paste(rejected, collapse = "; "), call. = FALSE)
  }
  records <- Filter(function(entry) identical(entry$status, "processed"), relevant)
  if (!length(records)) stop("No processed DEL inputs for ", config$source, call. = FALSE)
  paths <- vapply(records, function(entry) entry$output, character(1))
  # Binary copies share one artifact in the ELT log; consume that artifact once.
  records <- records[!duplicated(paths)]
  attr(records, "excluded_inputs") <- rejected
  records
}

as_model_date <- function(values) {
  if (inherits(values, "POSIXt")) return(as.Date(values, tz = "UTC"))
  if (inherits(values, "Date")) return(values)
  if (!is.character(values)) stop("DEL dates must be typed dates/timestamps or ISO strings", call. = FALSE)
  as.Date(values, format = "%Y-%m-%d")
}

weekday_names <- function(iso_weekdays) {
  c("Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo")[iso_weekdays]
}

check_export_overlap <- function(dates, input_files, config) {
  sources_per_day <- tapply(input_files, as.character(dates), function(files) length(unique(files)))
  overlapping <- names(sources_per_day)[sources_per_day > 1L]
  if (length(overlapping)) {
    description <- paste0("Overlapping DEL exports for ", config$source, " on ", length(overlapping),
                          " dates. Review export periods before summing sales.")
    if (isTRUE(config$reject_overlapping_dates)) stop(description, call. = FALSE)
    warning(description, " All rows will be retained.", call. = FALSE)
  }
}

read_del_sales <- function(config) {
  records <- select_del_records(config)
  tables <- list()
  input_files <- character()
  for (entry in records) {
    path <- file.path(config$repository_root, entry$output)
    if (!file.exists(path) || !path_is_within(path, config$input_directory)) {
      stop("Missing DEL artifact or path outside the configured source: ", entry$output, call. = FALSE)
    }
    checksum <- digest::digest(file = path, algo = "sha256", serialize = FALSE)
    if (!identical(checksum, entry$output_sha256)) {
      stop("DEL artifact changed after ELT: ", entry$output, ". Run ELT again.", call. = FALSE)
    }
    # Read each file separately: MUT exports have different optional schemas.
    raw <- as.data.frame(arrow::read_parquet(path, as_data_frame = TRUE))
    required <- c(config$date_column, config$product_column, config$quantity_column,
                  "source", "source_file", "source_sha256")
    if (length(config$paid_statuses)) required <- c(required, "status")
    if (!all(required %in% names(raw))) {
      stop("Missing model columns in ", entry$output, ": ",
           paste(setdiff(required, names(raw)), collapse = ", "), call. = FALSE)
    }
    units <- as.numeric(raw[[config$quantity_column]])
    dates <- as_model_date(raw[[config$date_column]])
    products <- as.character(raw[[config$product_column]])
    if (!nrow(raw) || anyNA(dates) || anyNA(products) || any(!nzchar(trimws(products))) ||
        any(!is.finite(units)) || any(units < 0)) {
      stop("Invalid model input values in ", entry$output, call. = FALSE)
    }
    if (anyNA(raw$source) || any(raw$source != config$source) ||
        anyNA(raw$source_sha256) || any(raw$source_sha256 != entry$source_sha256) ||
        nrow(raw) != entry$report$output_rows || sum(units) != entry$report$quantity_total) {
      stop("DEL provenance or totals do not match ELT: ", entry$output, call. = FALSE)
    }
    check_table <- data.frame(fecha = dates, producto = products, demanda = units,
                              input_file = entry$output, stringsAsFactors = FALSE)
    if (length(config$paid_statuses)) {
      accepted <- !is.na(raw$status) & raw$status %in% config$paid_statuses
      if (any(!accepted)) warning(sum(!accepted), " non-paid lines excluded from ", entry$output, call. = FALSE)
      check_table <- check_table[accepted, , drop = FALSE]
    }
    tables[[length(tables) + 1L]] <- check_table
    input_files <- c(input_files, entry$output)
    message("INFO: Loaded ", entry$output, " (", nrow(raw), " rows)")
  }
  data <- do.call(rbind, tables)
  if (!nrow(data)) stop("No accepted sales remain for modeling", call. = FALSE)
  check_export_overlap(data$fecha, data$input_file, config)
  active <- as.POSIXlt(data$fecha, tz = "UTC")$wday %in% (config$operating_weekdays %% 7L)
  if (any(!active)) message("INFO: Excluded ", sum(!active), " lines outside configured operating weekdays")
  data <- data[active & data$demanda > 0, c("fecha", "producto", "demanda"), drop = FALSE]
  if (!nrow(data)) stop("No positive sales on the configured operating weekdays", call. = FALSE)
  list(data = data, input_files = input_files,
       excluded_inputs = attr(records, "excluded_inputs"))
}

load_model_context <- function(site, repository_root) {
  config <- read_model_config(site, repository_root)
  inputs <- read_del_sales(config)
  list(config = config, data = inputs$data, input_files = inputs$input_files,
       excluded_inputs = inputs$excluded_inputs)
}

prepare_output_directories <- function(config) {
  if (!dir.exists(config$output_directory) &&
      !dir.create(config$output_directory, recursive = TRUE, showWarnings = FALSE)) {
    stop("Could not create model output directory: ", config$output_directory, call. = FALSE)
  }
  if (isTRUE(config$generate_plots) && !dir.exists(config$graphs_directory) &&
      !dir.create(config$graphs_directory, recursive = TRUE, showWarnings = FALSE)) {
    stop("Could not create graph output directory: ", config$graphs_directory, call. = FALSE)
  }
}

write_analysis_outputs <- function(tables, context) {
  config <- context$config
  prepare_output_directories(config)
  if (isTRUE(config$export_parquet)) {
    for (name in names(tables)) {
      path <- file.path(config$output_directory, paste0(tolower(name), ".parquet"))
      arrow::write_parquet(tables[[name]], path, compression = "snappy")
      message("INFO: Saved ", path)
    }
  }
  workbook <- NULL
  if (isTRUE(config$export_excel)) {
    workbook <- file.path(config$output_directory, config$workbook_filename)
    writexl::write_xlsx(tables, path = workbook)
    message("INFO: Saved ", workbook)
  }
  snapshot <- c(config, list(input_files = context$input_files,
                             excluded_inputs = context$excluded_inputs,
                             generated_at = format(Sys.time(), tz = "UTC", usetz = TRUE)))
  jsonlite::write_json(snapshot, file.path(config$output_directory, "config_used.json"),
                       auto_unbox = TRUE, pretty = TRUE, null = "null")
  invisible(workbook)
}

# Draw a line only between consecutive calendar days: a day without data or forecast in between
# (e.g. Franklin's Monday-Thursday) breaks the line instead of joining Sunday to Friday.
lines_by_day <- function(x, y, ...) {
  order_x <- order(x)
  x <- x[order_x]
  y <- y[order_x]
  segment <- cumsum(c(TRUE, diff(as.numeric(x)) > 1))
  for (part in unique(segment)) {
    keep <- segment == part
    if (sum(keep) > 1) lines(x[keep], y[keep], ...) else points(x[keep], y[keep], pch = 16, cex = 0.6, ...)
  }
  invisible(NULL)
}
