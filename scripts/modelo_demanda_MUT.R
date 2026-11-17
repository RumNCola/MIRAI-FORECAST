
# MIRAI | Pronostico diario por producto - MUT
# Instalar una vez: install.packages(c("readxl", "writexl"))

# 1. CONFIGURACION -------------------------------------------------------
ARCHIVO <- "C:/Users/cataa/Downloads/Mirai_scripts_R_descarga/demanda MUT consolidada.xlsx"
HOJA <- "Demanda Limpia"
COL_FECHA <- "Fecha"
COL_PRODUCTO <- "Producto"
COL_CANTIDAD <- "Cantidad"
SALIDA <- "C:/Users/cataa/Downloads/Mirai_scripts_R_descarga/resultados/mut"

SOLO_DIA <- TRUE    # TRUE: demanda ~ dia | FALSE: demanda ~ dia + mes
HORIZONTE <- 28     # Dias calendario futuros
MODELO <- if (SOLO_DIA) "dia" else "dia_mes_anual"

# 2. LEER DATOS ----------------------------------------------------------
if (!requireNamespace("readxl", quietly = TRUE) ||
    !requireNamespace("writexl", quietly = TRUE)) {
  stop("Instala readxl y writexl")
}

raw <- readxl::read_excel(ARCHIVO, sheet = HOJA)
if (!all(c(COL_FECHA, COL_PRODUCTO, COL_CANTIDAD) %in% names(raw))) {
  stop("Revisa los nombres de las columnas")
}

fecha <- raw[[COL_FECHA]]
if (is.numeric(fecha)) fecha <- as.Date(fecha, origin = "1899-12-30")
datos <- data.frame(
  fecha = as.Date(fecha),
  producto = trimws(as.character(raw[[COL_PRODUCTO]])),
  demanda = as.numeric(raw[[COL_CANTIDAD]])
)
if (anyNA(datos) || any(datos$producto == "") || any(datos$demanda < 0)) {
  stop("El Excel contiene datos invalidos")
}

# MUT: se consideran lunes a domingo; el archivo ya esta limpio.
datos <- datos[datos$demanda > 0, ]
if (!nrow(datos)) stop("No hay ventas para modelar")

ventas <- aggregate(demanda ~ fecha + producto, datos, sum)
fechas_local <- sort(unique(ventas$fecha))
productos <- sort(unique(ventas$producto))

dias <- c("Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo")
nombre_dia <- function(f) {
  c("Domingo", "Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado")[
    as.POSIXlt(f)$wday + 1]
}
calendario <- function(f) {
  data.frame(
    fecha = f,
    dia = factor(nombre_dia(f), levels = dias),
    mes = factor(format(f, "%m"), levels = sprintf("%02d", 1:12))
  )
}

formula_modelo <- if (SOLO_DIA) demanda ~ dia else demanda ~ dia + mes
fechas_futuras <- seq(max(fechas_local) + 1, by = "day", length.out = HORIZONTE)
futuro <- calendario(fechas_futuras)

# 3. REGRESION POR PRODUCTO ----------------------------------------------
dir.create(SALIDA, recursive = TRUE, showWarnings = FALSE)
CARPETA_GRAFICOS <- file.path(SALIDA, "graficos")
dir.create(CARPETA_GRAFICOS, showWarnings = FALSE)

pronosticos <- list(); metricas <- list(); historico <- list()
coeficientes <- list(); residuos_dia <- list()

for (i in seq_along(productos)) {
  p <- productos[i]
  v <- ventas[ventas$producto == p, ]
  fechas <- fechas_local[fechas_local >= min(v$fecha)]
  df <- calendario(fechas)
  df$demanda <- v$demanda[match(fechas, v$fecha)]
  df$demanda[is.na(df$demanda)] <- 0  # Solo en fechas con ventas del local
  
  # Se usa el modelo indicado, sin seleccion por BIC.
  # Si no es estimable, se intenta dia y finalmente promedio.
  elegido <- MODELO
  fit <- NULL
  for (opcion in unique(c(MODELO, "dia"))) {
    formula <- if (opcion == "dia") demanda ~ dia else formula_modelo
    intento <- tryCatch(lm(formula, data = df), error = function(e) NULL)
    if (is.null(intento) || anyNA(coef(intento))) next
    if (nrow(df) <= length(coef(intento)) + 2) next
    pred <- tryCatch(suppressWarnings(predict(intento, newdata = futuro)),
                     error = function(e) NULL)
    if (is.null(pred) || any(!is.finite(pred))) next
    fit <- intento
    elegido <- opcion
    break
  }
  
  if (!is.null(fit)) {
    estimado <- as.numeric(fitted(fit))
    proyectado <- pmax(0, as.numeric(predict(fit, newdata = futuro)))
    r2 <- summary(fit)$r.squared
    r2_aj <- summary(fit)$adj.r.squared
    sigma <- summary(fit)$sigma
    coeficientes[[length(coeficientes) + 1]] <- data.frame(
      producto = p, modelo = elegido,
      termino = names(coef(fit)), coeficiente = unname(coef(fit))
    )
  } else {
    elegido <- "promedio"
    estimado <- rep(mean(df$demanda), nrow(df))
    proyectado <- rep(mean(df$demanda), nrow(futuro))
    r2 <- r2_aj <- NA_real_
    sigma <- if (nrow(df) > 1) sd(df$demanda) else NA_real_
  }
  if (elegido != MODELO) {
    message("Aviso: ", p, " -> ", elegido,
            " (no fue posible estimar ", MODELO, ")")
  }
  
  # 4. ERRORES E INCERTIDUMBRE -------------------------------------------
  residuo <- df$demanda - estimado
  mae <- mean(abs(residuo))
  rmse <- sqrt(mean(residuo^2))
  wape <- 100 * sum(abs(residuo)) / sum(df$demanda)
  
  # Variabilidad residual por dia, si existen al menos 5 observaciones.
  sd_por_dia <- sapply(dias, function(d) {
    e <- residuo[df$dia == d]
    if (length(e) >= 5) sd(e) else NA_real_
  })
  residuos_dia[[i]] <- data.frame(
    producto = p, dia = dias,
    N = sapply(dias, function(d) sum(df$dia == d)),
    sesgo_medio = sapply(dias, function(d) {
      e <- residuo[df$dia == d]
      if (length(e)) mean(e) else NA_real_
    }),
    sigma_residuos_dia = unname(sd_por_dia),
    varianza_residuos_dia = unname(sd_por_dia^2)
  )
  
  sigma_dia_futura <- unname(sd_por_dia[as.character(futuro$dia)])
  origen_sigma <- ifelse(is.na(sigma_dia_futura), "global", "por_dia")
  sigma_utilizada <- ifelse(is.na(sigma_dia_futura), sigma, sigma_dia_futura)
  
  metricas[[i]] <- data.frame(
    producto = p, modelo_solicitado = MODELO, modelo_aplicado = elegido,
    N = nrow(df), unidades_vendidas = sum(df$demanda),
    R2 = round(r2, 3), R2_ajustado = round(r2_aj, 3),
    MAE = round(mae, 2), RMSE = round(rmse, 2),
    WAPE_pct = round(wape, 1),
    sigma_residuos = round(sigma, 2),
    varianza_residuos = round(sigma^2, 2)
  )
  historico[[i]] <- data.frame(
    fecha = df$fecha, producto = p, dia = as.character(df$dia),
    mes = as.integer(as.character(df$mes)),
    observado = df$demanda, ajustado = round(estimado, 2),
    residuo = round(residuo, 2), modelo_aplicado = elegido
  )
  pronosticos[[i]] <- data.frame(
    fecha = futuro$fecha, producto = p, dia = as.character(futuro$dia),
    mes = as.integer(as.character(futuro$mes)),
    demanda_predicha = round(proyectado, 2),
    RMSE_historico = round(rmse, 2),
    sigma_global = round(sigma, 2),
    sigma_dia = round(sigma_dia_futura, 2),
    sigma_utilizada = round(sigma_utilizada, 2),
    varianza_utilizada = round(sigma_utilizada^2, 2),
    origen_sigma = origen_sigma, modelo_aplicado = elegido
  )
  
  # 5. GRAFICOS: UNO POR PRODUCTO ----------------------------------------
  nombre <- substr(gsub("[^A-Za-z0-9_-]", "_", p), 1, 65)
  png(file.path(CARPETA_GRAFICOS, sprintf("%02d_%s.png", i, nombre)),
      width = 1800, height = 1350, res = 150)
  par(mfrow = c(3, 2), mar = c(4, 4, 2.5, 1), oma = c(0, 0, 2.5, 0))
  
  plot(df$fecha, df$demanda, pch = 16, cex = 0.45, col = "grey60",
       ylim = range(c(0, df$demanda, estimado)),
       xlab = "Fecha", ylab = "Unidades", main = "Historico: real vs ajustado")
  lines(df$fecha, estimado, col = "steelblue", lwd = 2)
  legend("topleft", c("Real", "Ajustado"),
         col = c("grey60", "steelblue"), pch = c(16, NA),
         lty = c(0, 1), bty = "n", cex = 0.8)
  
  grupos <- function(variable, titulo) {
    g <- as.character(variable)
    niveles <- levels(variable)[levels(variable) %in% g]
    real <- sapply(niveles, function(z) mean(df$demanda[g == z]))
    ajust <- sapply(niveles, function(z) mean(estimado[g == z]))
    barplot(rbind(Real = real, Ajustado = ajust), beside = TRUE,
            col = c("grey70", "steelblue"), main = titulo,
            ylab = "Unidades promedio", las = 2, cex.names = 0.75)
    legend("topright", c("Real", "Ajustado"),
           fill = c("grey70", "steelblue"), bty = "n", cex = 0.7)
  }
  grupos(df$dia, "Promedio por dia")
  grupos(df$mes, "Promedio por mes")
  
  limites <- range(c(0, df$demanda, estimado))
  plot(estimado, df$demanda, pch = 16, cex = 0.45,
       col = "grey50", xlim = limites, ylim = limites,
       xlab = "Ajustado", ylab = "Real", main = "Real vs ajustado")
  abline(0, 1, col = "firebrick", lty = 2)
  
  plot(df$fecha, residuo, pch = 16, cex = 0.5,
       col = "grey50", xlab = "Fecha", ylab = "Real - ajustado",
       main = "Errores historicos")
  abline(h = 0, col = "firebrick", lty = 2)
  
  ult <- which(df$fecha >= max(df$fecha) - 45)
  plot(range(c(df$fecha[ult], futuro$fecha)),
       range(c(0, df$demanda[ult], estimado[ult], proyectado)),
       type = "n", xlab = "Fecha", ylab = "Unidades",
       main = "Ultimos 45 dias y pronostico")
  points(df$fecha[ult], df$demanda[ult], pch = 16, cex = 0.5, col = "grey60")
  lines(df$fecha[ult], estimado[ult], col = "steelblue", lwd = 2)
  lines(futuro$fecha, proyectado, col = "firebrick", lwd = 2, lty = 2)
  legend("topleft", c("Real", "Ajustado", "Pronostico"),
         col = c("grey60", "steelblue", "firebrick"),
         pch = c(16, NA, NA), lty = c(0, 1, 2), bty = "n", cex = 0.8)
  
  mtext(paste0(p, " | ", elegido, " | MAE: ", round(mae, 2),
               " | WAPE: ", round(wape, 1), "%"),
        side = 3, outer = TRUE, line = 0.6, font = 2, cex = 0.9)
  dev.off()
}

# 6. EXCEL Y RESUMEN EN CONSOLA ------------------------------------------
pronosticos <- do.call(rbind, pronosticos)
metricas <- do.call(rbind, metricas)
historico <- do.call(rbind, historico)
residuos_dia <- do.call(rbind, residuos_dia)
coeficientes <- if (length(coeficientes)) {
  do.call(rbind, coeficientes)
} else {
  data.frame(producto = character(), modelo = character(),
             termino = character(), coeficiente = numeric())
}

configuracion <- data.frame(
  parametro = c("Local", "Archivo", "Hoja", "Solo dia", "Modelo solicitado",
                "Modelo dia", "Modelo dia_mes_anual", "Horizonte (dias)",
                "Dias de operacion", "Criterio de estimacion"),
  valor = c("MUT", basename(ARCHIVO), HOJA, SOLO_DIA, MODELO,
            "demanda ~ dia", "demanda ~ dia + mes", HORIZONTE,
            "Lunes a domingo", "Modelo fijo; sin seleccion por BIC")
)
archivo_salida <- file.path(SALIDA, "modelo_demanda_MUT.xlsx")
writexl::write_xlsx(
  list(Configuracion = configuracion, Pronosticos = pronosticos,
       Metricas = metricas, Historico = historico,
       Residuos_por_dia = residuos_dia, Coeficientes = coeficientes),
  path = archivo_salida
)

cat("\n========== MUT: RESUMEN ==========\n")
cat("Modelo solicitado:", MODELO, "\n")
cat("Productos:", nrow(metricas),
    "| Fechas del local:", length(fechas_local), "\n")
cat("Modelos aplicados:\n")
print(table(metricas$modelo_aplicado))
cat("\nMetricas por producto (ajuste dentro de muestra):\n")
resumen <- metricas[order(-metricas$unidades_vendidas),
                    c("producto", "modelo_aplicado", "N", "unidades_vendidas",
                      "R2_ajustado", "MAE", "RMSE", "WAPE_pct",
                      "sigma_residuos", "varianza_residuos")]
print(resumen, row.names = FALSE)
cat("\nDesviacion residual por dia (6 productos mas vendidos):\n")
top <- head(metricas$producto[order(-metricas$unidades_vendidas)], 6)
print(residuos_dia[residuos_dia$producto %in% top,
                   c("producto", "dia", "N", "sigma_residuos_dia")],
      row.names = FALSE)
cat("\nWAPE global:",
    round(100 * sum(abs(historico$residuo)) / sum(historico$observado), 1), "%\n")
cat("\nExcel:", archivo_salida, "\nGraficos:", CARPETA_GRAFICOS, "\n")
