# Setup cron-job.org → GitHub Actions

Patrón: cron-job.org cada 5 min hace `POST` al endpoint
`workflow_dispatch` de la GitHub API. Esto evita la cola interna de
schedules de GitHub Actions y dispara el workflow on-demand.

## Paso 1 — generar PAT (Personal Access Token)

1. https://github.com/settings/tokens?type=beta — "Fine-grained tokens".
2. **Repository access**: solo `MendozaVolcanic/mirova-tif-archive`.
3. **Permissions** → Repository:
   - `Actions` → **Read and write**
   - `Contents` → **Read and write** (para que el workflow commitee)
   - `Metadata` → Read (default).
4. Expiration: 1 año.
5. Copiar el token `github_pat_...`.

(Como alternativa: usar el `gho_*` de tu sesión `gh auth status`, ya tiene
`workflow` scope. Pero un token dedicado al repo es más seguro y revocable
por separado si se filtra.)

## Paso 2 — crear el cronjob en cron-job.org

1. https://cron-job.org → Cronjobs → "Create cronjob".
2. **Title**: `mirova-tif-archive poll`
3. **URL**:
   ```
   https://api.github.com/repos/MendozaVolcanic/mirova-tif-archive/actions/workflows/poll.yml/dispatches
   ```
4. **Schedule**: Every 5 minutes (interval).
5. **Advanced** → **Request method**: `POST`.
6. **Advanced** → **Request headers**:
   ```
   Accept: application/vnd.github+json
   Authorization: Bearer <TU_PAT>
   X-GitHub-Api-Version: 2022-11-28
   Content-Type: application/json
   ```
7. **Advanced** → **Request body**:
   ```json
   {"ref":"main"}
   ```
8. **Notifications** → activar email al failures (no a cada éxito).
9. **Save & enable**.

## Paso 3 — verificar

- En cron-job.org, ejecutar el job manualmente con "Test run". Esperar
  HTTP `204` (no content) — eso significa que GitHub aceptó el dispatch.
- En GitHub: ir a la pestaña **Actions** del repo y ver que apareció un
  run nuevo de `Poll MIROVA TIF/KMZ`.
- El primer run pobla `data/` con todos los TIF/KMZ actuales (33 archivos
  en el peor caso). A partir del segundo run cada 5 min, solo descarga
  cuando algún `Last-Modified` cambió.

## Paso 4 — monitoreo continuo

- Activar notificación de email en cron-job.org si tres ejecuciones
  consecutivas fallan.
- Revisar `index.csv` semanalmente — si una combinación (volcán × sensor)
  no aparece nunca, probablemente MIROVA cambió la URL de ese asset y hay
  que actualizar `polling/poll.py`.

## Si cron-job.org falla

Plan B: cron interno de GitHub Actions con `schedule:` cada 5 min.
Riesgo: queue puede demorar (~15 min) en horas pico, pero es funcional.
Ya hay infra preparada en el workflow — solo descomentar el bloque
`schedule:` que se agregue cuando sea necesario.

## Costos

- cron-job.org: tier gratuito permite 50 cronjobs y ejecución cada 1 min.
  Suficiente.
- GitHub Actions: repo público = minutos ilimitados. 5 min × 12/h × 24h =
  288 runs/día, cada uno ~30s típico → ~3h CPU/día.
