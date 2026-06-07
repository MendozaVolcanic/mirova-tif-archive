# mirova-tif-archive

Archivo histórico de **GeoTIFF + KMZ** scrapeados de
[mirovaweb.it](https://www.mirovaweb.it/) cada 5 minutos para los
**11 volcanes chilenos Tier A** monitoreados por MIROVA, en los 3 sensores
de infrarrojo medio (MIR):

| Sensor   | Banda | URL pattern (`{V}` = nombre MIROVA)                                                |
| -------- | ----- | ---------------------------------------------------------------------------------- |
| MODIS    | B21   | `https://www.mirovaweb.it/OUTPUTweb/MIROVA/MODIS/VOLCANOES/{V}/{V}_MODIS_B21.tif`  |
| VIIRS750 | M13   | `…/VIIRS750/VOLCANOES/{V}/{V}_VIIRS750_M13.tif`                                    |
| VIIRS375 | I04   | `…/VIIRS375/VOLCANOES/{V}/{V}_VIIRS375_I04.tif`                                    |

KMZ análogo: `…/{V}_{SENSOR}_Last_GE.kmz`.

## Por qué este repo

- En `mirovaweb.it` los archivos TIF/KMZ son **sobreescritos en cada nueva
  pasada del satélite** (~3-6 h). Sin scraping, el histórico se pierde.
- `latest.php` (que ya scrapea
  [Mirova-v1](https://github.com/MendozaVolcanic/Mirova-v1)) reporta solo
  **1 detección por burst**: si un sensor pasa varias veces en pocos
  minutos, ~80% de las pasadas no quedan registradas. El TIF, en cambio, se
  reescribe en cada pasada — por polling Last-Modified capturamos las que
  `latest.php` se pierde.
- TIF + KMZ permiten **verificación pixel-level** vs nuestra implementación
  en [VRP-chile](https://github.com/MendozaVolcanic/VRP-chile)
  (regla R2 de `docs/PROCESS_RULES_S33.md`).

## Estructura

```
mirova-tif-archive/
├── config/
│   └── volcanoes.yml          # 11 Tier A con id MIROVA y URL stem
├── polling/
│   ├── poll.py                # script de polling Last-Modified
│   └── requirements.txt
├── data/
│   ├── tif/{Volcano}/{YYYYMMDD_HHMMSS}_{sensor}.tif
│   └── kmz/{Volcano}/{YYYYMMDD_HHMMSS}_{sensor}.kmz
├── index.csv                  # log: vol, sensor, last_modified, md5, file_path
├── .github/workflows/poll.yml # schedule nativo */30min (S103) + workflow_dispatch
└── README.md
```

`index.csv` es la fuente de verdad sobre qué se descargó cuándo. El
timestamp del nombre de archivo es el **acquisition time del satélite**
(cuándo el sensor adquirió la imagen), parseado del header de
`volcanoMap.php`. Esto es distinto del HTTP `Last-Modified` del TIF —
MIROVA puede republicar la misma adquisición múltiples veces con
distintos `Last-Modified`, así que naming por adquisición es lo correcto.

Columnas del index:
- `captured_at_utc`: cuándo nuestro polling hizo el download.
- `acquisition_utc`: cuándo el satélite tomó la imagen (parseado de
  `volcanoMap.php` "Last Update"). Vacío para filas legacy pre-fix.
- `last_modified_utc`: HTTP Last-Modified del archivo en MIROVA al
  momento del download.
- `md5`: hash del contenido del TIF. Filas con mismo md5 = misma
  adquisición republicada.

## Cómo se actualiza

1. **Trigger** (dos vías, conviven):
   - **Schedule nativo de GitHub Actions** (`poll.yml`, `*/30 * * * *`), agregado
     en **S103 (2026-06-07)** como respaldo durable. El trigger externo de abajo
     murió silenciosamente el **2026-05-20** y el archivo quedó congelado ~18 días
     sin que nadie lo notara (lo detectó la auditoría S103 de VRP Chile). El
     schedule nativo no depende de ningún servicio externo. GitHub puede correrlo
     tarde en picos, pero para validación R2 (TIF ±90 min de una detección) cada
     ~30 min alcanza. **Caveat**: MIROVA *sobrescribe* sus TIF en cada pasada → el
     polling solo captura hacia adelante; el histórico previo a una reactivación
     es irrecuperable.
   - **Trigger externo (opcional)**: [cron-job.org](https://cron-job.org/) cada 5
     min hace `POST` a la GitHub API (más frecuente que el schedule nativo):
     ```
     POST https://api.github.com/repos/MendozaVolcanic/mirova-tif-archive/actions/workflows/poll.yml/dispatches
     Authorization: Bearer <PAT>
     {"ref":"main"}
     ```
     Esto evita la cola/latencia del cron interno de GitHub. Si se revive, ambos
     triggers conviven (el `concurrency: poll` serializa las corridas solapadas).
2. **Workflow `poll.yml`**: corre `polling/poll.py`, commit + push si hay
   archivos nuevos.
3. **`poll.py`**:
   - HEAD request al TIF de cada combinación (11 vol × 3 sens = 33).
   - Si `Last-Modified` cambió vs la última fila de `index.csv`:
     - GET TIF + GET KMZ.
     - MD5 del TIF; si igual al MD5 anterior → descarta (defensa contra
       reescritura idempotente).
     - Si MD5 distinto → guarda en `data/`, agrega fila a `index.csv`.

## Cuotas y mantenimiento

- Volumen estimado: ~10-12 pasadas/día/volcán × 11 vol × ~70 KB promedio
  ≈ **8 MB/día** = 240 MB/mes. Manageable en Git sin LFS.
- Si en 12 meses crece >5 GB: migrar a Git LFS o partir por trimestre.
- HEAD requests: 33 × 12/h × 24 h = 9500/día. Carga despreciable para
  mirovaweb.it.

## Volcanes monitoreados

| MIROVA ID | Nombre código MIROVA | Nombre común     |
| --------- | -------------------- | ---------------- |
| 355030    | Isluga               | Isluga           |
| 355100    | Lascar               | Láscar           |
| 355120    | Lastarria            | Lastarria        |
| 357010    | Tupungatito          | Tupungatito      |
| 357040    | PlanchonPeteroa      | Planchón-Peteroa |
| 357070    | ChillanNevadosde     | Nevados de Chillán |
| 357090    | Copahue              | Copahue          |
| 357110    | Llaima               | Llaima           |
| 357120    | Villarrica           | Villarrica       |
| 357150    | PuyehueCordonCaulle  | Puyehue-Cordón Caulle |
| 358041    | Chaiten              | Chaitén          |

## Atribución

Datos fuente: **MIROVA — Middle Infrared Observations of Volcanic Activity**,
Universidad de Turín, Italia. Contacto: diego.coppola@unito.it.
[mirovaweb.it](https://www.mirovaweb.it/)

Este repo es un mirror del producto público de MIROVA con fines de
investigación científica (replicación metodológica) en SERNAGEOMIN Chile.
No redistribuye el servicio, ni interfiere con su operación.
