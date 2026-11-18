# MIRAI | Inventory model by ingredient - FRANKLIN and MUT
# Shared settings: settings/config.py (InventorySettings, InventorySiteSettings).
# Inputs: the "Pronosticos" sheet of each demand workbook written by modelo_demanda_*.R in the
# same run, plus settings/inventario_parametros.xlsx (sheets vida_util and composicion).
# Assumptions and sources: scripts/SUPUESTOS_modelo_inventario.md

script_path <- local({
  sourced <- vapply(sys.frames(), function(frame) {
    if (is.null(frame$ofile)) "" else as.character(frame$ofile)
  }, character(1))
  sourced <- sourced[nzchar(sourced)]
  if (length(sourced)) tail(sourced, 1L) else {
    argument <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
    if (length(argument) != 1L) stop("Run this script with Rscript or source()")
    sub("^--file=", "", argument)
  }
})
repository_root <- normalizePath(file.path(dirname(script_path), ".."), winslash = "/", mustWork = TRUE)
source(file.path(repository_root, "scripts", "demand_io.R"), local = TRUE)

# 1. CONFIGURATION ---------------------------------------------------------------------
read_inventory_config <- function(repository_root) {
  require_model_packages("jsonlite")
  path <- Sys.getenv("MIRAI_INVENTORY_CONFIG", unset = "")
  text <- if (nzchar(path)) {
    paste(readLines(path, warn = FALSE, encoding = "UTF-8"), collapse = "\n")
  } else {
    python <- Sys.getenv("MIRAI_PYTHON", unset = "")
    if (!nzchar(python)) {
      candidates <- c(file.path(repository_root, ".venv", "Scripts", "python.exe"),
                      file.path(repository_root, ".venv", "bin", "python"),
                      unname(Sys.which(c("python", "python3"))))
      candidates <- candidates[nzchar(candidates) & file.exists(candidates)]
      if (!length(candidates)) stop("Python was not found; prepare the repository environment first", call. = FALSE)
      python <- candidates[[1]]
    }
    result <- suppressWarnings(system2(python, c(shQuote(file.path(repository_root, "settings", "config.py")),
                                                 "--inventory"), stdout = TRUE))
    if (!is.null(attr(result, "status")) && attr(result, "status") != 0L) {
      stop("Could not load the inventory settings from settings/config.py", call. = FALSE)
    }
    paste(result, collapse = "\n")
  }
  config <- jsonlite::fromJSON(text, simplifyVector = TRUE, simplifyDataFrame = FALSE)
  if (!identical(config$schema_version, 1L) || !identical(config$model, "inventory")) {
    stop("Unexpected inventory configuration contract", call. = FALSE)
  }
  require_model_packages(config$required_r_packages)
  config
}

CONFIG <- read_inventory_config(repository_root)
DIAS <- c("Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo")
iso_weekday <- function(f) as.POSIXlt(f)$wday + ifelse(as.POSIXlt(f)$wday == 0L, 7L, 0L)
day_name <- function(f) DIAS[iso_weekday(f)]
week_start <- function(f) f - (iso_weekday(f) - 1L)

# Accent-, case- and spacing-insensitive names, identical on Windows and Linux.
normalize_name <- function(x) {
  x <- enc2utf8(as.character(x))
  accents <- c("á" = "a", "é" = "e", "í" = "i", "ó" = "o", "ú" = "u", "ü" = "u",
               "ñ" = "n", "Á" = "A", "É" = "E", "Í" = "I", "Ó" = "O", "Ú" = "U",
               "Ü" = "U", "Ñ" = "N", "à" = "a", "è" = "e", "ì" = "i", "ò" = "o",
               "ù" = "u", "ś" = "s")
  # Byte-wise replacement works under any session locale (UTF-8 or C).
  for (accent in names(accents)) {
    x <- gsub(enc2utf8(accent), accents[[accent]], x, fixed = TRUE, useBytes = TRUE)
  }
  x <- gsub("[^A-Za-z0-9/]+", " ", x, useBytes = TRUE)
  x <- toupper(x)
  trimws(gsub(" +", " ", x, useBytes = TRUE))
}

# 2. PARAMETERS WORKBOOK ---------------------------------------------------------------
# Excel cells are read as text: accept a decimal comma ("2,5"), as the interface validator does.
parse_number <- function(x) suppressWarnings(as.numeric(sub(",", ".", trimws(as.character(x)), fixed = TRUE)))

read_parameters <- function(config) {
  path <- config$parameters_workbook
  if (!file.exists(path)) stop("Inventory parameters workbook not found: ", path, call. = FALSE)
  sheets <- readxl::excel_sheets(path)
  tables <- list()
  for (sheet in names(config$parameter_sheets)) {
    columns <- unlist(config$parameter_sheets[[sheet]])
    if (!sheet %in% sheets) stop("Sheet '", sheet, "' is missing in ", basename(path), call. = FALSE)
    table <- as.data.frame(readxl::read_excel(path, sheet = sheet, col_types = "text"))
    if (!all(columns %in% names(table))) {
      stop("Sheet '", sheet, "' needs the columns: ", paste(columns, collapse = ", "), call. = FALSE)
    }
    table <- table[, columns, drop = FALSE]
    tables[[sheet]] <- table[rowSums(!is.na(table)) > 0, , drop = FALSE]
  }
  vida <- tables$vida_util
  vida$ingrediente <- trimws(vida$ingrediente)
  shelf_text <- trimws(as.character(vida$vida_util_dias))
  vida$vida_util_dias <- parse_number(shelf_text)
  if (anyNA(vida$ingrediente) || any(!nzchar(vida$ingrediente))) stop("vida_util: empty ingredient name", call. = FALSE)
  if (any(duplicated(tolower(vida$ingrediente)))) {
    stop("vida_util: repeated ingredients: ", paste(unique(vida$ingrediente[duplicated(tolower(vida$ingrediente))]),
                                                     collapse = ", "), call. = FALSE)
  }
  # A typed value must be a finite positive number: text, NaN and Inf are rejected, not taken as empty.
  bad_shelf <- !is.na(shelf_text) & nzchar(shelf_text) &
    (is.na(vida$vida_util_dias) | !is.finite(vida$vida_util_dias) | vida$vida_util_dias <= 0)
  if (any(bad_shelf)) {
    stop("vida_util: vida_util_dias must be a positive number or empty (", paste(vida$ingrediente[bad_shelf],
         collapse = ", "), ")", call. = FALSE)
  }
  comp <- tables$composicion
  comp$local <- toupper(trimws(comp$local))
  comp$ingrediente <- trimws(comp$ingrediente)
  comp$cantidad_por_plato <- parse_number(comp$cantidad_por_plato)
  if (anyNA(comp$producto) || anyNA(comp$ingrediente) || any(!comp$local %in% c("FRANKLIN", "MUT", "AMBOS"))) {
    stop("composicion: every row needs local (FRANKLIN, MUT or AMBOS), producto and ingrediente", call. = FALSE)
  }
  if (anyNA(comp$cantidad_por_plato) || any(!is.finite(comp$cantidad_por_plato) | comp$cantidad_por_plato <= 0)) {
    stop("composicion: cantidad_por_plato must be a positive number", call. = FALSE)
  }
  unknown <- setdiff(tolower(comp$ingrediente), tolower(vida$ingrediente))
  if (length(unknown)) {
    stop("composicion uses ingredients missing from vida_util: ", paste(unknown, collapse = ", "), call. = FALSE)
  }
  comp$clave <- normalize_name(comp$producto)
  repeated <- duplicated(data.frame(comp$local, comp$clave, tolower(comp$ingrediente)))
  if (any(repeated)) {
    stop("composicion: the same local, producto and ingrediente appear more than once: ",
         paste(unique(paste0(comp$producto[repeated], " / ", comp$ingrediente[repeated], " (", comp$local[repeated], ")")),
               collapse = "; "), call. = FALSE)
  }
  list(vida = vida, comp = comp)
}

# 3. DEMAND FORECASTS --------------------------------------------------------------------
read_forecasts <- function(site) {
  path <- site$demand_workbook
  if (!file.exists(path)) stop("Demand workbook of ", site$site, " not found: ", path, call. = FALSE)
  raw <- as.data.frame(readxl::read_excel(path, sheet = "Pronosticos"))
  required <- c("fecha", "producto", "demanda_predicha", "sigma_utilizada")
  if (!all(required %in% names(raw))) stop("Pronosticos sheet lacks: ",
                                           paste(setdiff(required, names(raw)), collapse = ", "), call. = FALSE)
  out <- data.frame(fecha = as.Date(raw$fecha), producto = as.character(raw$producto),
                    mu = as.numeric(raw$demanda_predicha), sigma = as.numeric(raw$sigma_utilizada),
                    stringsAsFactors = FALSE)
  if (anyNA(out$fecha) || anyNA(out$mu)) stop("Invalid forecasts in ", path, call. = FALSE)
  # Unknown spread (e.g. a product with a single observation): never assume zero uncertainty;
  # use the Poisson approximation for counts, variance = expected demand.
  unknown_sigma <- is.na(out$sigma) | !is.finite(out$sigma)
  out$sigma[unknown_sigma] <- sqrt(pmax(out$mu[unknown_sigma], 0))
  # Same rule as the production report: a product without sales in the last inactive_days days of
  # the site's data is discontinued or renamed, although the demand model still forecasts it.
  out$activo <- TRUE
  if (!is.null(CONFIG$inactive_days) && "Historico" %in% readxl::excel_sheets(path)) {
    history <- as.data.frame(readxl::read_excel(path, sheet = "Historico"))
    if (all(c("fecha", "producto", "observado") %in% names(history)) && nrow(history)) {
      history$fecha <- as.Date(history$fecha)
      last <- max(history$fecha, na.rm = TRUE)
      recent <- history$fecha > last - CONFIG$inactive_days & suppressWarnings(as.numeric(history$observado)) > 0
      # Exact names, as in the report: the demand model forecasts each spelling from its own history,
      # so an old spelling stays inactive even when a similar new one sells.
      out$activo <- out$producto %in% history$producto[recent %in% TRUE]
    }
  }
  out$clave <- normalize_name(out$producto)
  out
}

# 4. POLICY HELPERS ------------------------------------------------------------------------
standard_loss <- function(k) dnorm(k) - k * (1 - pnorm(k))
expected_units <- function(S, mu, sig) {
  if (sig <= 0) return(c(faltante = max(mu - S, 0), sobrante = max(S - mu, 0)))
  short <- sig * standard_loss((S - mu) / sig)
  c(faltante = short, sobrante = S - mu + short)
}

# Each delivery covers the opening days until the next delivery. Day index 1 = delivery day.
build_cycles <- function(forecast_dates, site) {
  start <- min(forecast_dates); end <- max(forecast_dates)
  calendar <- seq(start, end + 21, by = "day")
  delivery <- iso_weekday(calendar) %in% site$delivery_weekdays
  opening <- iso_weekday(calendar) %in% site$operating_weekdays
  deliveries <- calendar[delivery & calendar <= end]
  cycles <- list()
  for (e in as.list(deliveries)) {
    following <- min(calendar[delivery & calendar > e])
    days <- forecast_dates[forecast_dates >= e & forecast_dates < following]
    if (!length(days)) next
    next_use <- min(calendar[calendar >= following & opening])
    cycles[[length(cycles) + 1L]] <- list(
      delivery = e, days = days, idx = as.numeric(days - e) + 1,
      last_idx = as.numeric(max(days) - e) + 1, next_use_idx = as.numeric(next_use - e) + 1,
      truncated = any(calendar > end & calendar < following & opening))
  }
  cycles
}

# 5. MODEL ---------------------------------------------------------------------------------
parameters <- read_parameters(CONFIG)
vida <- parameters$vida
z_ci <- qnorm(1 - (1 - CONFIG$confidence_level) / 2)
policy <- list(); weekly <- list(); weekly_dish <- list(); unmatched <- list(); uncovered <- list()
inactive <- list()

for (site in CONFIG$sites) {
  forecasts <- read_forecasts(site)
  if (any(!forecasts$activo)) {
    gone <- forecasts[!forecasts$activo, ]
    inactive[[site$site]] <- aggregate(mu ~ producto, gone, sum)
    inactive[[site$site]]$local <- site$site
    message("Aviso: ", length(unique(gone$producto)), " productos de ", site$site, " sin ventas en los ultimos ",
            CONFIG$inactive_days, " dias quedan fuera del inventario")
    forecasts <- forecasts[forecasts$activo, ]
  }
  comp <- parameters$comp[parameters$comp$local %in% c(site$site, "AMBOS"), ]
  # A row for this site overrides the AMBOS row of the same dish and ingredient, whatever the row order.
  comp <- comp[order(comp$local == "AMBOS"), ]
  comp <- comp[!duplicated(data.frame(comp$clave, tolower(comp$ingrediente))), ]
  matched <- merge(forecasts, comp[, c("clave", "ingrediente", "cantidad_por_plato")], by = "clave")
  missing <- setdiff(unique(forecasts$clave), comp$clave)
  if (length(missing) && nrow(forecasts)) {
    lost <- forecasts[forecasts$clave %in% missing, ]
    unmatched[[site$site]] <- aggregate(mu ~ producto, lost, sum)
    unmatched[[site$site]]$local <- site$site
    message("Aviso: ", length(missing), " productos de ", site$site,
            " no tienen composicion y quedan fuera del inventario")
  }
  if (!nrow(matched)) {
    message("Aviso: ningun producto de ", site$site, " calza con la hoja composicion")
    next
  }
  matched$mu_ing <- matched$mu * matched$cantidad_por_plato
  matched$var_ing <- (matched$sigma * matched$cantidad_por_plato)^2
  matched$semana <- week_start(matched$fecha)

  # Weekly view: dishes that use each ingredient, with a normal confidence interval.
  by_dish <- aggregate(cbind(mu, mu_ing, var_ing) ~ semana + ingrediente + producto, matched, sum)
  by_dish$local <- site$site
  weekly_dish[[site$site]] <- by_dish
  daily <- aggregate(cbind(mu_ing, var_ing) ~ fecha + ingrediente, matched, sum)
  dishes <- aggregate(producto ~ ingrediente, matched, function(x) paste(sort(unique(x)), collapse = "; "))
  week <- aggregate(cbind(mu_ing, var_ing) ~ semana + ingrediente, matched, sum)
  week_days <- aggregate(fecha ~ semana, unique(matched[, c("semana", "fecha")]), length)
  week$dias_en_horizonte <- week_days$fecha[match(week$semana, week_days$semana)]
  week$local <- site$site
  week$platos_que_lo_usan <- dishes$producto[match(week$ingrediente, dishes$ingrediente)]
  weekly[[site$site]] <- week

  # Replenishment cycles per ingredient: perishable newsvendor with expiry-based overage cost.
  forecast_dates <- sort(unique(forecasts$fecha))
  cycles <- build_cycles(forecast_dates, site)
  first_delivery <- if (length(cycles)) cycles[[1]]$delivery else max(forecast_dates) + 1
  before <- forecast_dates[forecast_dates < first_delivery]
  if (length(before)) {
    uncovered[[site$site]] <- data.frame(local = site$site, fecha = as.character(before),
                                         motivo = "Dia de apertura previo a la primera entrega del horizonte")
  }
  for (ing in sort(unique(daily$ingrediente))) {
    d <- daily[daily$ingrediente == ing, ]
    row <- match(tolower(ing), tolower(vida$ingrediente))
    shelf <- vida$vida_util_dias[row]
    # Preparations are finished on the dispatch day: shelf life runs from that day (day 1).
    usable <- shelf
    perishable <- !is.na(shelf)
    carried <- 0
    for (k in seq_along(cycles)) {
      cc <- cycles[[k]]
      pos <- match(cc$days, d$fecha)
      mu_d <- ifelse(is.na(pos), 0, d$mu_ing[pos]); var_d <- ifelse(is.na(pos), 0, d$var_ing[pos])
      mu <- sum(mu_d); sig <- sqrt(sum(var_d))
      valid <- if (perishable) cc$idx <= usable else rep(TRUE, length(cc$idx))
      capped <- perishable && any(!valid)
      destination <- "no_perecible"; Co <- NA_real_; h <- NA_real_
      if (perishable && usable < 1) {
        destination <- "vence_antes_de_entrega"; Co <- 1
      } else if (perishable) {
        h <- 1 / usable
        if (cc$next_use_idx <= usable) {
          destination <- "siguiente_ciclo"; Co <- h * (cc$next_use_idx - cc$last_idx)
        } else if (isTRUE(site$transfer_leftovers) && cc$last_idx + 1 <= usable) {
          destination <- "transferencia"; Co <- h
        } else {
          destination <- "merma"; Co <- 1
        }
      }
      Cu <- site$shortage_cost
      fractile <- if (perishable) Cu / (Cu + Co) else CONFIG$default_service_level
      fractile <- min(max(fractile, CONFIG$min_fractile), CONFIG$max_fractile)
      z <- qnorm(fractile)
      S <- ceiling(max(0, mu + z * sig))
      if (capped) S <- min(S, ceiling(max(0, sum(mu_d[valid]) + z * sqrt(sum(var_d[valid])))))
      ex <- expected_units(S, mu, sig)
      order <- max(0, S - floor(carried))
      policy[[length(policy) + 1L]] <- data.frame(
        local = site$site, ingrediente = ing, ciclo = k,
        fecha_entrega = as.character(cc$delivery), dia_entrega = day_name(cc$delivery),
        fecha_fin_cobertura = as.character(max(cc$days)), dias_cubiertos = length(cc$days),
        cobertura_truncada = cc$truncated,
        demanda_esperada_ciclo = round(mu, 2), sigma_ciclo = round(sig, 2),
        vida_util_dias = shelf,
        fuente_vida_util = vida$fuente[row],
        restriccion_vida_util = capped,
        demanda_fuera_vida_util = round(mu - sum(mu_d[valid]), 2),
        reposiciones_intermedias = if (capped && usable >= 1) ceiling(cc$last_idx / usable) - 1 else 0,
        destino_sobrante = destination,
        costo_mantencion_dia = round(h, 4), costo_sobrante_Co = round(Co, 4), costo_quiebre_Cu = Cu,
        fractil_critico = round(fractile, 4), z = round(z, 3),
        stock_objetivo = S, inventario_inicial_esperado = floor(carried), pedido_sugerido = order,
        faltante_esperado = round(ex[["faltante"]], 2), sobrante_esperado = round(ex[["sobrante"]], 2),
        fill_rate_esperado = round(if (mu > 0) 1 - ex[["faltante"]] / mu else 1, 4),
        merma_esperada = round(if (destination %in% c("merma", "vence_antes_de_entrega")) ex[["sobrante"]] else 0, 2),
        transferencia_esperada = round(if (destination == "transferencia") ex[["sobrante"]] else 0, 2),
        stringsAsFactors = FALSE)
      carried <- if (destination %in% c("siguiente_ciclo", "no_perecible")) ex[["sobrante"]] else 0
    }
  }
}

# 6. OUTPUT TABLES -------------------------------------------------------------------------
empty_policy <- data.frame(local = character(), ingrediente = character(), fecha_entrega = character(),
                           pedido_sugerido = numeric(), stock_objetivo = numeric())
politica <- if (length(policy)) do.call(rbind, policy) else empty_policy
semanal <- if (length(weekly)) do.call(rbind, weekly) else NULL
if (!is.null(semanal)) {
  semanal$sd <- sqrt(semanal$var_ing)
  prepared <- if (nrow(politica)) {
    p <- politica
    p$semana <- as.character(week_start(as.Date(p$fecha_entrega)))
    aggregate(cbind(pedido_sugerido, stock_objetivo) ~ local + ingrediente + semana, p, sum)
  } else NULL
  semanal <- data.frame(
    local = semanal$local, semana_inicio = as.character(semanal$semana),
    semana_fin = as.character(semanal$semana + 6), dias_en_horizonte = semanal$dias_en_horizonte,
    ingrediente = semanal$ingrediente,
    porciones_esperadas = round(semanal$mu_ing, 1), desviacion_estandar = round(semanal$sd, 2),
    nivel_confianza = CONFIG$confidence_level,
    ic_inferior = round(pmax(0, semanal$mu_ing - z_ci * semanal$sd), 1),
    ic_superior = round(semanal$mu_ing + z_ci * semanal$sd, 1),
    vida_util_dias = vida$vida_util_dias[match(tolower(semanal$ingrediente), tolower(vida$ingrediente))],
    platos_que_lo_usan = semanal$platos_que_lo_usan, stringsAsFactors = FALSE)
  if (!is.null(prepared)) {
    key <- paste(semanal$local, semanal$ingrediente, semanal$semana_inicio)
    pkey <- paste(prepared$local, prepared$ingrediente, prepared$semana)
    semanal$porciones_a_preparar <- prepared$pedido_sugerido[match(key, pkey)]
    semanal$porciones_a_preparar[is.na(semanal$porciones_a_preparar)] <- 0
  }
  semanal <- semanal[order(semanal$local, semanal$semana_inicio, -semanal$porciones_esperadas), ]
} else {
  semanal <- data.frame(local = character(), semana_inicio = character(), ingrediente = character(),
                        porciones_esperadas = numeric())
}
detalle <- if (length(weekly_dish)) do.call(rbind, weekly_dish) else NULL
detalle <- if (!is.null(detalle)) data.frame(
  local = detalle$local, semana_inicio = as.character(detalle$semana), ingrediente = detalle$ingrediente,
  producto = detalle$producto, platos_esperados = round(detalle$mu, 2),
  porciones_esperadas = round(detalle$mu_ing, 2),
  desviacion_estandar = round(sqrt(detalle$var_ing), 2), stringsAsFactors = FALSE) else
  data.frame(local = character(), semana_inicio = character(), ingrediente = character(), producto = character(),
             platos_esperados = numeric(), porciones_esperadas = numeric(), desviacion_estandar = numeric())
detalle <- detalle[order(detalle$local, detalle$semana_inicio, detalle$ingrediente, -detalle$porciones_esperadas), ]
sin_composicion <- if (length(unmatched)) {
  x <- do.call(rbind, unmatched)
  data.frame(local = x$local, producto = x$producto, demanda_horizonte_excluida = round(x$mu, 1))
} else data.frame(local = character(), producto = character(), demanda_horizonte_excluida = numeric())
sin_ventas <- if (length(inactive)) {
  x <- do.call(rbind, inactive)
  data.frame(local = x$local, producto = x$producto, demanda_horizonte_excluida = round(x$mu, 1))
} else data.frame(local = character(), producto = character(), demanda_horizonte_excluida = numeric())
sin_cobertura <- if (length(uncovered)) do.call(rbind, uncovered) else
  data.frame(local = character(), fecha = character(), motivo = character())

parametros <- data.frame(
  parametro = c("nivel_confianza", "nivel_servicio_defecto", "fractil_min", "fractil_max", "archivo_parametros",
                unlist(lapply(CONFIG$sites, function(s) paste0(tolower(s$site), "_",
                  c("dias_entrega", "costo_quiebre_rel", "transferir_sobrante",
                    "dias_apertura", "horizonte_dias", "modelo_demanda"))))),
  valor = c(CONFIG$confidence_level, CONFIG$default_service_level, CONFIG$min_fractile, CONFIG$max_fractile,
            basename(CONFIG$parameters_workbook),
            unlist(lapply(CONFIG$sites, function(s) c(
              paste(DIAS[s$delivery_weekdays], collapse = ", "), s$shortage_cost,
              s$transfer_leftovers, paste(DIAS[s$operating_weekdays], collapse = ", "), s$horizon_days,
              s$demand_model)))),
  stringsAsFactors = FALSE)

diccionario <- data.frame(
  columna = c("porciones_esperadas", "platos_esperados", "desviacion_estandar", "ic_inferior / ic_superior", "porciones_a_preparar",
              "fecha_entrega", "vida_util_dias", "stock_objetivo", "pedido_sugerido",
              "inventario_inicial_esperado", "restriccion_vida_util", "demanda_fuera_vida_util",
              "destino_sobrante", "costo_sobrante_Co", "costo_quiebre_Cu", "fractil_critico",
              "fill_rate_esperado", "merma_esperada", "transferencia_esperada"),
  definicion = c(
    "Porciones del ingrediente: suma en la semana de demanda predicha x cantidad_por_plato de los platos que lo usan",
    "Solo en Detalle_semanal_plato: platos pronosticados del producto en la semana (sin multiplicar por cantidad_por_plato)",
    "Raiz de la suma de varianzas diarias de esos platos (independencia entre dias y platos)",
    "porciones_esperadas -/+ z * desviacion_estandar con el nivel de confianza elegido (inferior acotado en 0)",
    "Suma de pedido_sugerido de los despachos de esa semana",
    "Dia de despacho: la preparacion queda terminada y disponible en el local ese dia",
    "Dias utilizables contados desde el termino de la preparacion (= dia de despacho, dia 1)",
    "Porciones a tener al inicio del ciclo (order-up-to)",
    "max(0, stock_objetivo - inventario_inicial_esperado)",
    "Sobrante esperado del ciclo anterior que sigue vigente (FIFO)",
    "TRUE si la vida util no alcanza a cubrir todos los dias del ciclo",
    "Demanda de dias que la entrega no alcanza a cubrir por vida util (reposicion intermedia)",
    "siguiente_ciclo, transferencia, merma, vence_antes_de_entrega o no_perecible",
    "Costo de una porcion sobrante: fraccion de vida util consumida esperando, o 1 si vence",
    "Costo de una porcion faltante relativo al costo de una porcion",
    "Cu / (Cu + Co) acotado; nivel de servicio por defecto si no hay vida util",
    "1 - faltante_esperado / demanda_esperada_ciclo (sin reposicion intermedia)",
    "Sobrante esperado que vence antes de su proximo uso",
    "Sobrante esperado enviado a otro local al dia siguiente del ciclo"),
  stringsAsFactors = FALSE)

tables <- list(Parametros = parametros, Resumen_semanal = semanal, Detalle_semanal_plato = detalle,
               Politica_ingrediente = politica, Productos_sin_composicion = sin_composicion,
               Productos_sin_ventas_recientes = sin_ventas,
               Dias_sin_cobertura = sin_cobertura, Composicion_usada = parameters$comp[, c("local", "producto",
               "ingrediente", "cantidad_por_plato", "fuente")], Vida_util_usada = vida, Diccionario = diccionario)

if (!dir.exists(CONFIG$output_directory) && !dir.create(CONFIG$output_directory, recursive = TRUE)) {
  stop("Could not create inventory output directory: ", CONFIG$output_directory, call. = FALSE)
}
if (isTRUE(CONFIG$export_parquet)) {
  for (name in names(tables)) {
    arrow::write_parquet(tables[[name]], file.path(CONFIG$output_directory, paste0(tolower(name), ".parquet")))
  }
}
workbook <- NULL
if (isTRUE(CONFIG$export_excel)) {
  workbook <- file.path(CONFIG$output_directory, CONFIG$workbook_filename)
  writexl::write_xlsx(tables, path = workbook)
  message("INFO: Saved ", workbook)
}
jsonlite::write_json(c(CONFIG, list(generated_at = format(Sys.time(), tz = "UTC", usetz = TRUE))),
                     file.path(CONFIG$output_directory, "config_used.json"),
                     auto_unbox = TRUE, pretty = TRUE, null = "null")

cat("\n========== INVENTARIO: RESUMEN ==========\n")
cat("Ingredientes:", length(unique(politica$ingrediente)), "| Ciclos:", nrow(politica),
    "| Productos sin composicion:", nrow(sin_composicion), "\n")
if (nrow(semanal)) print(head(semanal[, c("local", "semana_inicio", "ingrediente", "porciones_esperadas",
                                          "ic_inferior", "ic_superior")], 12), row.names = FALSE)
cat("\nResultados:", CONFIG$output_directory, "\n")
