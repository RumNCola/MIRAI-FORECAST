# Install missing R packages into the project library, or check them without changes.
arguments <- commandArgs(trailingOnly = TRUE)
if (length(arguments) < 4L || !arguments[[1]] %in% c("install", "check")) {
  stop("Usage: prepare_r.R install|check library_directory CRAN_mirror package...", call. = FALSE)
}
mode <- arguments[[1]]
library_directory <- arguments[[2]]
mirror <- arguments[[3]]
packages <- arguments[-(1:3)]
if (mode == "install" && !dir.exists(library_directory)) {
  if (!dir.create(library_directory, recursive = TRUE, showWarnings = FALSE)) {
    stop("Could not create the project R package library", call. = FALSE)
  }
}
if (dir.exists(library_directory)) .libPaths(c(library_directory, .libPaths()))
options(repos = c(CRAN = mirror), timeout = 600)
missing <- packages[!vapply(packages, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing) && mode == "install") {
  message("INFO: Installing R packages: ", paste(missing, collapse = ", "))
  install.packages(missing, lib = library_directory,
                   type = if (.Platform$OS.type == "windows") "binary" else "source")
}
missing <- packages[!vapply(packages, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing)) {
  stop("R packages are unavailable: ", paste(missing, collapse = ", "),
       ". Run main.bat and check pipeline.log.", call. = FALSE)
}
message("INFO: R packages verified: ", paste(packages, collapse = ", "))
