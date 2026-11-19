# MIRAI-FORECAST

Pipeline local para transformar ventas de **Franklin y MUT** en pronósticos diarios por producto, sugerencias de inventario por ingrediente y un reporte de producción. Python coordina la ejecución, procesa datos y genera la interfaz y el PDF; R ejecuta los modelos estadísticos.

## Ejecución

Requiere **Windows y Python 3.12 o superior**. Desde la raíz del repositorio:

```powershell
.\main.bat                 # Ejecuta el pipeline con los datos locales
.\iniciar_interfaz.bat     # Abre la interfaz Streamlit para cargar archivos
```

Si `main.bat` se abre con doble clic, la ventana espera una tecla al terminar para que se puedan leer los mensajes; desde una terminal no espera. Para tareas programadas, define `MIRAI_NO_PAUSE=1`.

Ambos lanzadores crean o reutilizan `.venv`, instalan las dependencias de `requirements.txt` y preparan R y sus paquetes. La primera preparación requiere Internet; las instalaciones locales de R y su biblioteca quedan en `.runtime/` cuando corresponde.

Con el entorno preparado, se puede ejecutar directamente:

```powershell
.venv\Scripts\python.exe main.py
.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

La interfaz permite seleccionar locales, cargar Excel, ajustar parámetros y descargar resultados. Cada carga se aísla en `out/ejecuciones/<identificador>/`, con sus propios originales, datos procesados, configuración y salidas.

## Flujo computacional

```text
Ventas RAW → ELT Python → Parquet DEL → Demanda R por local
                                      → Reporte PDF Python → Inventario R
```

`main.py:run_pipeline()` valida configuración y dependencias, ejecuta el ELT y lanza los scripts R como subprocesos, en orden: **Franklin → MUT → Tienda Franklin → reporte → inventario**. El reporte va antes del inventario porque solo usa los pronósticos: un error en el Excel de parámetros detiene el inventario, pero no deja a cocina sin su PDF. Python y R intercambian configuración mediante JSON y resultados mediante archivos.

1. **ELT (`src/elt.py`).** Lee Excel con Openpyxl y transforma tablas con Polars. Valida hojas, columnas, fechas, cantidades y controles disponibles; conserva nombres de productos y añade procedencia por fila. Publica Parquet con compresión Snappy en DEL. El procesamiento es incremental: usa hashes SHA-256 y `elt_log.json` para omitir entradas sin cambios y reconstruir salidas faltantes o alteradas.
2. **Demanda (`scripts/modelo_demanda_*.R`).** Lee únicamente artefactos aceptados por el ELT, verificando su integridad mediante `scripts/demand_io.R`. Suma unidades por fecha y producto y ajusta una regresión lineal independiente por producto y local. Por defecto, MUT y la Tienda Franklin usan `demanda ~ dia` y Franklin `demanda ~ dia + mes`, con día y mes categóricos. En la tienda (`modelo_demanda_Franklin_Store.R`, local `FRANKLIN_STORE`) cada producto es nombre + variante (por ejemplo, `KOMBUCHA 330CC - Hibiscus`) y solo cuentan las líneas `Venta`: las notas de crédito (devoluciones y anulaciones, con cantidades negativas) no son demanda. Para la tienda se eligió `dia` porque, en una prueba con las últimas 4 semanas de octubre de 2026, erró 482 unidades en los 12 productos principales frente a 763 con `dia + mes` (solo hay un noviembre para estimar el efecto del mes). Si el ajuste o la proyección no son posibles, intenta un modelo solo por día y luego un promedio.
3. **Inventario (`scripts/modelo_inventario.R`).** Cruza la hoja `Pronosticos` con la composición de platos para convertir demanda en cantidades de ingredientes. Acumula medias y varianzas por semana y ciclo de despacho. Calcula un stock objetivo con una política *newsvendor* (equilibrio entre costo de faltantes y sobrantes), considerando vida útil, días de entrega y sobrantes esperados. La vida útil cuenta desde el día de despacho, cuando termina la preparación. `porciones_esperadas` son porciones del ingrediente (platos × `cantidad_por_plato`); el detalle por plato muestra también `platos_esperados`. En `composicion`, una fila de un local reemplaza a la fila `AMBOS` del mismo plato e ingrediente, sin importar el orden; repetir el mismo local, plato e ingrediente es un error. Igual que el reporte, deja fuera los productos sin ventas recientes (hoja `Productos_sin_ventas_recientes`).
4. **Producción (`src/report.py`).** Usa pronósticos e históricos de los dos restaurantes y la tienda (se genera solo si los tres corrieron en la misma ejecución) y agrupa productos según `settings/reporte_grupos.xlsx`. Genera un PDF por semana, con ciclos viernes–lunes y martes–jueves. La cantidad a producir por local, variante y ciclo es `ceil(sum(mu) + z * sqrt(sum(sigma²)))`, donde `z` depende del nivel de servicio configurado. Por defecto, la semana empieza el próximo viernes; `main.bat --comenzar-hoy` permite iniciarla hoy. Los productos sin ventas en los últimos 14 días de datos de un local (`INACTIVE_PRODUCT_DAYS`, compartido con el inventario) no se producen y se listan al final del PDF, porque los modelos siguen pronosticando platos retirados o renombrados.

El horizonte predeterminado es de **28 días calendario desde la última venta de cada local**, filtrados por sus días de apertura: MUT de lunes a domingo y Franklin de viernes a domingo. Desde la primera venta de un producto, su ausencia en una fecha con ventas del local se completa con cero; no se inventan días sin registros del local. Los pronósticos negativos se truncan a cero.

## Entradas y configuración

Para ejecutar por consola, colocar las exportaciones en estas carpetas:

| Entrada | Ubicación / formato |
|---|---|
| Ventas Franklin | `Data/RAW/FRANKLIN_SALES/`, Excel con hoja `Detalle` |
| Ventas MUT | `Data/RAW/MUT_SALES/`, Excel con hoja `Sheet1` |
| Ventas Tienda Franklin | `Data/RAW/FRANKLIN_STORE/`, Excel con hoja `Detalle de ventas` |
| Parámetros de ingredientes | `settings/inventario_parametros.xlsx`, hojas `vida_util` y `composicion` |
| Grupos del PDF | `settings/reporte_grupos.xlsx`, hoja `grupos` |

Los Excel sueltos directamente en `Data/RAW` no se procesan: el ELT se detiene y lista cada archivo con la carpeta a la que debe moverse.

El ELT admite `.xlsx`, `.xlsm` y `.parquet`; las cargas de la interfaz admiten Excel.  En el Excel de parámetros se aceptan decimales con coma (`2,5`); los valores no finitos (`NaN`, `inf`) se rechazan. MUT filtra por defecto las ventas con estado `pagado`. Los modelos rechazan períodos superpuestos entre archivos por defecto; no se eliminan automáticamente líneas de venta repetidas.

`settings/config.py` centraliza rutas, paquetes y parámetros en `ModelSettings`, `InventorySettings` e `InventorySiteSettings`. Los valores guardados desde la interfaz en `settings/valores_por_defecto.json` prevalecen sobre los valores del código para las opciones que contienen. Los scripts R también consumen esa configuración. Para inspeccionar el contrato JSON:

```powershell
.venv\Scripts\python.exe settings/config.py --site MUT
.venv\Scripts\python.exe settings/config.py --inventory
```

## Salidas y diagnóstico

Las rutas siguientes son relativas a la raíz del repositorio al ejecutar por consola; en la interfaz, son relativas a la carpeta de cada ejecución:

| Salida | Contenido |
|---|---|
| `Data/DEL/<fuente>/` | Tablas de ventas validadas en Parquet |
| `out/franklin/`, `out/mut/` | Excel, Parquet, gráficos por producto y `config_used.json` |
| `out/inventario/` | `modelo_inventario.xlsx`, tablas Parquet y configuración utilizada |
| `out/reporte/` | PDF y `estado.json` con estado y motivo de generación u omisión |
| `pipeline.log`, `elt.log`, `elt_log.json` | Registro de ejecución, eventos ELT y estado de procesamiento |

Los Excel de demanda incluyen `Pronosticos`, `Historico`, `Metricas`, `Residuos_por_dia`, `Coeficientes` y `Configuracion`. `sigma_utilizada` representa la desviación residual por día de semana si hay suficientes observaciones (cinco por defecto); en otro caso usa la global. Si no se puede estimar (por ejemplo, un producto con una sola venta), el inventario y el PDF usan la aproximación de Poisson (varianza = pronóstico) y nunca una incertidumbre cero.

Un error de modelo detiene las etapas posteriores y la consola devuelve código 1. Los archivos rechazados por el ELT permiten continuar con datos aceptados en consola; la interfaz detiene el análisis si alguno es rechazado. Un problema del PDF se registra sin invalidar los pronósticos. Los bloqueos `.pipeline.lock` y `.elt.lock` impiden ejecuciones simultáneas; eliminar un bloqueo residual solo después de comprobar que el proceso terminó.

## Orientación para mantenimiento

- **Orquestación y entorno:** `main.py`, `src/runtime.py`, `scripts/bootstrap.py`.
- **Carga, validación y limpieza:** `src/elt.py`; detalle en [docs/elt.md](docs/elt.md).
- **Modelos:** scripts R y utilidades `scripts/demand_io.R`; detalle en [manual de demanda](scripts/README_Mirai_GitHub.md). La política de inventario se implementa en `scripts/modelo_inventario.R`.
- **Interfaz y ejecuciones aisladas:** `streamlit_app.py` y `src/interface.py`.
- **PDF:** `src/report.py`.

Las pruebas usan datos sintéticos y se ejecutan con:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Las pruebas de integración R requieren el ejecutable y sus paquetes; `MIRAI_RSCRIPT` permite indicar una instalación específica. Las métricas de demanda son de ajuste histórico, sin validación temporal. El inventario y el PDF suman varianzas bajo supuestos de independencia y aproximación normal. Los datos, resultados y registros contienen información privada y deben permanecer fuera de Git.
