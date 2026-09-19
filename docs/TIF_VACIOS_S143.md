# TIF y PNG vacíos archivados antes de la validación (S143, 2026-09-19)

> Generado desde `index.csv` e `index_png.csv` de `main` al 2026-09-19. **No se borró nada**: borrar
> datos del archivo requiere el visto bueno de Nicolás. Arreglo que evita que vuelva a pasar:
> `contenido_valido()` en `polling/poll.py`, con sus pruebas en `tests/test_contenido_valido.py`.

## El fenómeno

MIROVA sobrescribe su imagen "Last" en cada pasada. Si el poller la pide justo mientras el servidor
la reescribe, recibe un 200 con el cuerpo vacío. Antes se archivaba igual, con el nombre de la hora de
la pasada. En la consulta siguiente, el TIF real de esa misma pasada chocaba con el vacío en el guard de
colisión y quedaba guardado con sufijo `_lm` (hora de modificación del servidor) y **sin hora de
adquisición**. El dato no se perdió en ningún caso, pero quedó mal rotulado.

**Para quien consuma el archivo**: el TIF bueno de cada una de estas pasadas es el de la columna
"captura siguiente"; su nombre NO es la hora de la pasada.

## TIF (index.csv)

| volcán | sensor | archivo vacío | captura siguiente (el TIF real) | bytes |
|---|---|---|---|---|
| Chaiten | VIIRS750 | `data/tif/Chaiten/20260706_233033_VIIRS750_lm.tif` | `data/tif/Chaiten/20260706_233034_VIIRS750_lm.tif` | 36822 |
| ChillanNevadosde | VIIRS375 | `data/tif/ChillanNevadosde/20260913_051801_VIIRS375.tif` | `data/tif/ChillanNevadosde/20260913_074634_VIIRS375_lm.tif` | 146196 |
| ChillanNevadosde | VIIRS750 | `data/tif/ChillanNevadosde/20260619_061202_VIIRS750.tif` | `data/tif/ChillanNevadosde/20260619_113044_VIIRS750_lm.tif` | 36986 |
| Copahue | MODIS | `data/tif/Copahue/20260704_075000_MODIS.tif` | `data/tif/Copahue/20260704_144514_MODIS_lm.tif` | 21462 |
| PlanchonPeteroa | MODIS | `data/tif/PlanchonPeteroa/20260909_093025_MODIS_lm.tif` | `data/tif/PlanchonPeteroa/20260909_074500_MODIS.tif` | 21694 |
| Tupungatito | VIIRS375 | `data/tif/Tupungatito/20260528_104701_VIIRS375_lm.tif` | `data/tif/Tupungatito/20260528_062402_VIIRS375.tif` | 146112 |
| Tupungatito | VIIRS375 | `data/tif/Tupungatito/20260627_192402_VIIRS375.tif` | `data/tif/Tupungatito/20260628_001637_VIIRS375_lm.tif` | 146160 |

Total: 7 TIF vacíos de 21365 filas del índice.

## PNG (index_png.csv)

| volcán | sensor | tipo | archivo vacío |
|---|---|---|---|
| ChillanNevadosde | COMB | logVRP | `data/png/ChillanNevadosde/20260606_203219_COMB_logVRP.png` |
| PlanchonPeteroa | COMB | VRP | `data/png/PlanchonPeteroa/20260724_221729_COMB_VRP.png` |

Total: 2 PNG vacíos de 132814 filas del índice.
