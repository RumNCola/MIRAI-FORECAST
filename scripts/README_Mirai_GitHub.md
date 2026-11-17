# Mirai | Pronóstico de demanda por producto

Scripts en **R** para estimar la demanda diaria de cada producto de **Franklin** y **MUT**. Los pronósticos y la variabilidad de sus errores se exportan a Excel para utilizarlos posteriormente en un modelo de planificación de producción e inventarios.

## 1. Archivos y ejecución

| Script | Local | Modelo inicial | Días considerados |
|---|---|---|---|
| `modelo_demanda_Franklin_sigma.R` | Franklin | `demanda ~ dia + mes` | Viernes, sábado y domingo |
| `modelo_demanda_MUT_sigma.R` | MUT | `demanda ~ dia` | Lunes a domingo |

**Dependencias:** `readxl` y `writexl` (el resto utiliza funciones de R base).

```r
install.packages(c("readxl", "writexl")) # Solo una vez
source("scripts/modelo_demanda_Franklin_sigma.R")
source("scripts/modelo_demanda_MUT_sigma.R")
```

Antes de ejecutar, editar la sección **CONFIGURACION** de cada script:

```r
ARCHIVO <- "datos/nombre_del_excel.xlsx"
HOJA <- "Nombre de la hoja"
COL_FECHA <- "Fecha"
COL_PRODUCTO <- "Producto"   # En Franklin puede ser "Categoria_Agrupada"
COL_CANTIDAD <- "Cantidad"  # En Franklin puede ser "Cantidades vendidas"
SALIDA <- "resultados/mut"  # Ajustar según el local
HORIZONTE <- 28             # Días calendario futuros
```

Se supone que los Excel **ya están limpios y estandarizados**. No se buscan archivos ni se identifican columnas automáticamente: se cambian esas variables si varían sus nombres.

## 2. Modelos y supuestos

Se ajusta **una regresión lineal por producto** usando todos los registros históricos disponibles; no hay separación entre entrenamiento y prueba ni selección por BIC.

- **Franklin:** `MODELO <- "dia_mes_anual"` (predeterminado) estima `demanda ~ dia + mes`; también permite `MODELO <- "dia"`. Se excluyen las observaciones extraordinarias de lunes a jueves.
- **MUT:** `SOLO_DIA <- TRUE` (predeterminado) estima `demanda ~ dia`. Cuando exista mayor historial, `SOLO_DIA <- FALSE` permite probar `demanda ~ dia + mes`.
- **Mes:** es una variable categórica de enero a diciembre, no una tendencia temporal. No se incluye semana del mes ni interacciones.
- **Productos:** las cantidades se suman por fecha y producto. Desde la primera venta observada de cada producto, su ausencia en una fecha con ventas del local se interpreta como **cero ventas**; no se inventan días de apertura sin registros.
- **Historial insuficiente:** si la especificación solicitada no puede estimarse o proyectarse, el script intenta `demanda ~ dia` y, en último caso, un promedio. Esto se informa en `modelo_aplicado`.
- **Pronóstico:** se generan los siguientes `HORIZONTE` días calendario (solo viernes a domingo en Franklin). Los pronósticos negativos se reemplazan por cero.

**Importante:** se están pronosticando *ventas observadas*, que pueden diferir de la demanda real si hubo quiebres de stock, cambios de carta o cierres no registrados. Los productos temporales o descontinuados requieren revisión antes de generar inventario.

## 3. Archivos generados

Cada script crea `modelo_demanda_Franklin.xlsx` o `modelo_demanda_MUT.xlsx` en la carpeta `SALIDA`, y una subcarpeta `graficos/` con **un PNG de seis paneles por producto** (histórico, promedios, dispersión, residuos y pronóstico).

| Hoja del Excel | Contenido |
|---|---|
| `Configuracion` | Archivo de entrada, horizonte y modelo solicitado |
| `Pronosticos` | Producto, fecha, día, mes, `demanda_predicha`, `sigma_utilizada`, `varianza_utilizada` y modelo aplicado |
| `Metricas` | Unidades vendidas, N, R², R² ajustado, MAE, RMSE, WAPE y varianza residual por producto |
| `Historico` | Ventas observadas, ajustes y residuos por fecha y producto |
| `Residuos_por_dia` | Número de observaciones, sesgo y desviación estándar residual por día de semana |
| `Coeficientes` | Parámetros estimados de cada regresión |

La consola muestra la cantidad de productos, modelos aplicados, métricas por producto, sigmas por día de los productos más vendidos y WAPE global.

### Interpretación de la incertidumbre

- `RMSE`: raíz del error cuadrático medio **dentro de muestra**.
- `sigma_residuos`: desviación estándar residual de la regresión, con corrección por grados de libertad; su cuadrado es `varianza_residuos`.
- `sigma_dia`: desviación estándar de los residuos de un día específico, calculada con al menos **5 observaciones**.
- `sigma_utilizada`: utiliza `sigma_dia` si se pudo calcular; en caso contrario, `sigma_global`. `origen_sigma` informa cuál se utilizó.

Estas estimaciones **no son intervalos de predicción validados**: no incorporan plenamente la incertidumbre de los coeficientes ni garantizan normalidad o independencia de los errores.

## 4. Regla de inventario sugerida (primera aproximación)

**Stock objetivo diario:** `S = techo(max(0, demanda_predicha + z * sigma_utilizada))`; **preparación necesaria:** `max(0, S - inventario_disponible)`, usando por ejemplo `z = 1,645` para un nivel de servicio de ciclo de 95 % *solo si* resulta razonable aproximar los errores diarios por una distribución normal.

Para abastecer **varios días con una sola preparación**, hay que modelar la demanda acumulada y su varianza; no basta con sumar las sigmas diarias. Antes de aplicar una política real también se deben considerar vida útil, costos de sobrantes/faltantes, capacidad y disponibilidad de insumos.

## 5. Calidad de ajuste y límites actuales

Referencia de los archivos de resultados revisados (noviembre de 2026):

| Métrica dentro de muestra | Franklin | MUT |
|---|---:|---:|
| Productos | 55 | 34 |
| WAPE global, ponderado por unidades vendidas | 30,9 % | 41,5 % |
| WAPE mediano por producto | 34,6 % | 67,6 % |
| R² ajustado mediano | 0,558 | 0,104 |

En Franklin los principales platos presentan ajustes razonables, especialmente sábados y domingos; los viernes tienen errores relativos mayores. MUT tiene un historial más corto y sus predicciones son menos estables para productos de baja rotación.

**Precaución:** todas estas métricas se calcularon sobre los mismos datos usados para estimar. No demuestran precisión futura. Para evaluar la capacidad de pronóstico se recomienda incorporar, cuando haya más registros, una validación temporal o retrospectiva; el modelo final puede volver a entrenarse luego con todos los datos disponibles.
