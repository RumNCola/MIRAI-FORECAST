# Mirai | Pronóstico de demanda por producto

Scripts en **R** para estimar la demanda diaria de cada producto de **Franklin** y **MUT**. Los pronósticos y la variabilidad de sus errores se exportan a Excel para utilizarlos posteriormente en un modelo de planificación de producción e inventarios.

## 1. Archivos y ejecución

| Script | Local | Modelo inicial | Días considerados |
|---|---|---|---|
| `modelo_demanda_Franklin.R` | Franklin | `demanda ~ dia + mes` | Viernes, sábado y domingo |
| `modelo_demanda_MUT.R` | MUT | `demanda ~ dia` | Lunes a domingo |

**Dependencias:** Python del repositorio, R y los paquetes `arrow`, `digest`,
`jsonlite` y `writexl`. La lista también está declarada en `R_PACKAGES`, dentro de
`settings/config.py`, para la futura preparación de dependencias desde `main.bat`.

```r
install.packages(c("arrow", "digest", "jsonlite", "writexl")) # Solo una vez
source("scripts/modelo_demanda_Franklin.R")
source("scripts/modelo_demanda_MUT.R")
```

También pueden ejecutarse como procesos independientes, que es el contrato para
el futuro `main.py`:

```powershell
Rscript scripts/modelo_demanda_Franklin.R
Rscript scripts/modelo_demanda_MUT.R
```

Los scripts localizan el repositorio desde su propio archivo, independientemente
del directorio de ejecución. Seleccionan Python desde `MIRAI_PYTHON`, si está
definido, o desde `.venv` y después el PATH. No instalan paquetes automáticamente.

### Configuración central

Editar únicamente `settings/config.py`: `HORIZON_DAYS` establece el horizonte
común y `MODEL_SETTINGS` contiene la configuración de cada local. Los modelos R
consumen un contrato JSON emitido por ese módulo. Se puede inspeccionar con:

```powershell
python settings/config.py --site FRANKLIN
python settings/config.py --site MUT
```

Las opciones incluyen `model`, `horizon_days`, `operating_weekdays` (lunes=1,
domingo=7), columnas de entrada, subcarpeta de salida, formatos de tablas,
generación y tamaño de gráficos, ventana histórica y mínimo de observaciones
para el sigma por día. Por defecto se generan tanto Excel como Parquet y PNG.
Las rutas de salida deben permanecer dentro de `out/`. `ModelSettings` y
`get_model_config()` también se pueden importar desde el futuro `main.py`.

### Entradas DEL

- Franklin usa exclusivamente `Data/DEL/FRANKLIN_SALES/`; no mezcla los movimientos
  y notas de crédito de `FRANKLIN_STORE`.
- MUT usa todos los archivos aceptados de `Data/DEL/MUT_SALES/`, aunque tengan
  distintas columnas opcionales. `MUT_STORE` se puede seleccionar explícitamente
  como fuente alternativa en la configuración.
- Se requiere `elt_log.json`, generado por el ELT. Se leen únicamente los
  artefactos con estado `processed`, una vez por ruta de salida. Se verifican
  SHA-256, procedencia, filas y unidades antes del modelado. Las entradas fallidas
  del ELT se excluyen con advertencia; los archivos ajenos al registro no entran
  al modelo.
- Las columnas predeterminadas son `date`, `product_name` y `quantity`. Se
  conservan los nombres originales. La antigua `Categoria_Agrupada` de Franklin
  no está en DEL: se modela cada `product_name`, sin reconstruir agrupaciones ni
  aplicar correcciones de nombres.
- MUT incluye solo estados de pago configurados (por defecto `pagado`).
  Las filas repetidas dentro de un archivo se conservan. Los períodos de archivos
  distintos que se superponen detienen el modelo por defecto; revisar la causa
  antes de desactivar `reject_overlapping_dates`.

## 2. Modelos y supuestos

Se ajusta **una regresión lineal por producto** usando todos los registros históricos disponibles; no hay separación entre entrenamiento y prueba ni selección por BIC.

- **Franklin:** `model="dia_mes_anual"` (predeterminado) estima `demanda ~ dia + mes`; también permite `model="dia"`. Se excluyen las observaciones extraordinarias de lunes a jueves según los días configurados.
- **MUT:** `model="dia"` (predeterminado) estima `demanda ~ dia`. `model="dia_mes_anual"` permite probar `demanda ~ dia + mes`.
- **Mes:** es una variable categórica de enero a diciembre, no una tendencia temporal. No se incluye semana del mes ni interacciones.
- **Productos:** las cantidades se suman por fecha y producto. Desde la primera venta observada de cada producto, su ausencia en una fecha con ventas del local se interpreta como **cero ventas**; no se inventan días de apertura sin registros.
- **Historial insuficiente:** si la especificación solicitada no puede estimarse o proyectarse, el script intenta `demanda ~ dia` y, en último caso, un promedio. Esto se informa en `modelo_aplicado`.
- **Pronóstico:** se generan los siguientes `HORIZONTE` días calendario (solo viernes a domingo en Franklin). Los pronósticos negativos se reemplazan por cero.

**Importante:** se están pronosticando *ventas observadas*, que pueden diferir de la demanda real si hubo quiebres de stock, cambios de carta o cierres no registrados. Los productos temporales o descontinuados requieren revisión antes de generar inventario.

## 3. Archivos generados

Cada script guarda sus resultados en `out/franklin/` o `out/mut/`, según la
configuración. Por defecto crea `modelo_demanda_Franklin.xlsx` o
`modelo_demanda_MUT.xlsx`, las seis tablas del análisis en archivos `.parquet`,
y una subcarpeta `graphs/` con **un PNG de seis paneles por producto** (histórico,
promedios, dispersión, residuos y pronóstico). `config_used.json` registra los
parámetros utilizados, archivos DEL y fecha de generación.

| Hoja del Excel | Contenido |
|---|---|
| `Configuracion` | Fuente y archivos DEL, horizonte y modelo solicitado |
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

Las métricas se recalculan con las entradas DEL y la configuración actual. Las
cifras de los antiguos Excel consolidados no describen estas nuevas entradas,
especialmente porque Franklin ya no usa la columna `Categoria_Agrupada`.

**Precaución:** todas estas métricas se calcularon sobre los mismos datos usados para estimar. No demuestran precisión futura. Para evaluar la capacidad de pronóstico se recomienda incorporar, cuando haya más registros, una validación temporal o retrospectiva; el modelo final puede volver a entrenarse luego con todos los datos disponibles.

## 6. Verificación

```powershell
python -m unittest discover -s tests -v
```

Las pruebas de integración de los modelos se ejecutan si encuentran `Rscript` y
sus paquetes. Se puede definir `MIRAI_RSCRIPT` con la ruta al ejecutable para las
pruebas. Cuando R no está instalado se informa explícitamente que esa verificación
se omite; las pruebas de configuración Python siguen ejecutándose. La validación
opcional de sintaxis usa `tree-sitter==0.21.3` y
`tree-sitter-languages==1.10.2`, sin requerir instalar R.
