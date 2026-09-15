# Fix: Celery beat container reported unhealthy (pgrep missing)

## What shipped
Add `procps` to the runtime image so the beat container's healthcheck works,
plus a `start_period` on the beat healthcheck. `timpbills_beat` was showing
`(unhealthy)` while running perfectly; it now reports healthy.

## Why (root cause)
The beat healthcheck is `pgrep -f 'celery.*beat' || exit 1`
(docker-compose.prod.yml). `pgrep` is provided by the `procps` package, which
`python:slim` does not ship, and the runtime stage installed only `libpq5`.
So inside the beat container `pgrep` did not exist: the check exited 127
(`/bin/sh: 1: pgrep: not found`) every run, and `|| exit 1` marked it unhealthy
on every interval. Beat itself was fine the whole time.

Confirmed live on the prod VPS:
- `docker inspect ... .State.Health.Log` -> `exit=1 :: /bin/sh: 1: pgrep: not found` (repeated).
- `docker exec timpbills_beat sh -lc 'command -v pgrep || echo PGREP-MISSING'` -> `PGREP-MISSING`.
- `docker top timpbills_beat` -> the `celery ... beat` process running.
- `docker compose logs beat` -> "beat: Starting..." and "Scheduler: Sending due task ..." every 2 min.

Why worker was healthy but beat was not: the worker healthcheck uses
`celery ... inspect ping` (celery is in the venv, no pgrep needed); only the
beat check depended on the missing `pgrep`. That asymmetry was the tell.

## How
- `Dockerfile` runtime stage: `apt-get install ... libpq5 procps` (was `libpq5`).
  Rebuilds the image so `pgrep` is present; the existing healthcheck then passes.
- `docker-compose.prod.yml` beat service: added `start_period: 30s` (worker has
  60s; beat had none) so it does not flap unhealthy during the boot window.

Rejected alternative: rewriting the healthcheck to a pgrep-free form (scan
`/proc/*/cmdline`). Installing `procps` is smaller, standard, and keeps the
intended check. procps adds a trivial amount to the image.

## What's involved
- `Dockerfile` (runtime apt install).
- `docker-compose.prod.yml` (beat healthcheck `start_period`).

## Verification
- `python3 -c "import yaml; yaml.safe_load(open('docker-compose.prod.yml'))"` -> OK.
- After deploy, on the box: `docker compose ps` shows `timpbills_beat` as
  `Up (healthy)`; `docker exec timpbills_beat sh -lc 'command -v pgrep'` resolves.

## Operate / roll back
Revert the two edits and redeploy; beat returns to a cosmetically-unhealthy
(but functional) state. No data or behavior change either way; this only fixes
the health signal.

## Follow-ups
None. Note the beat "unhealthy" state never blocked deploys (the deploy gate
watches API health), so there was no outage, only a misleading signal.
