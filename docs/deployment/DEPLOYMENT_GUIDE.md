# Deployment Guide — Timpbills API

Practical reference for running Timpbills API on a Docker Compose–managed VPS, both for local development and production.

## Compose files

- `docker-compose.yml` — base, development-friendly defaults (bind mounts, exposed ports).
- `docker-compose.prod.yml` — production override. Shorter, because it only contains what differs from base.

Production runs both, merged:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Or, if your `docker-compose.prod.yml` is self-contained:

```bash
docker compose -f docker-compose.prod.yml up -d
```

## Local development (laptop)

Run with fewer workers to keep the laptop cool:

```bash
API_WORKERS=2 docker compose up -d
# memory: ~1.5–2 GB total
```

Or bake it into `.env`:

```bash
echo "API_WORKERS=2" >> .env
docker compose up -d
```

## Production (8 GB VPS)

Default to 4 workers. Handles ~80–120 req/sec comfortably.

```bash
docker compose -f docker-compose.prod.yml up -d
# memory: ~3–4 GB with 4 workers
```

Scale recommendation:

| Server RAM | `API_WORKERS` | Expected load |
|---|---|---|
| 2 GB | 2 | 0–50 users |
| 4 GB | 3 | 50–200 users |
| 8 GB | 4 | 100–300 concurrent sessions |
| 16 GB+ | 6–8 | 500+ users |

## Production bootstrap (Ubuntu/Debian)

```bash
# 1. Install Docker (includes Compose v2)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # log out/in after
docker compose version

# 2. Clone the repo
sudo mkdir -p /opt/timpbills
sudo chown "$USER:$USER" /opt/timpbills
cd /opt/timpbills
git clone https://github.com/<owner>/<repo>.git .

# 3. Create a strong .env
cp .env.example .env
# Generate good secrets:
#   openssl rand -hex 32      # SECRET_KEY
#   openssl rand -base64 32   # POSTGRES_PASSWORD
#   openssl rand -base64 24   # admin password (12+ chars)
nano .env
chmod 600 .env        # /opt is world-readable by default

# 4. Start the stack
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps

# 5. Apply migrations
docker compose -f docker-compose.prod.yml exec api alembic upgrade head

# 6. Smoke test
curl http://localhost:8000/api/v1/health
```

## Nginx reverse proxy (recommended)

```bash
sudo apt install nginx
sudo nano /etc/nginx/sites-available/timpbills
```

```nginx
server {
    listen 80;
    server_name api.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/timpbills /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## SSL with Let's Encrypt

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d api.example.com
```

Certbot will edit your nginx config to add the HTTPS server block and renew automatically.

## Automatic database backups (installed by CD)

The CD pipeline runs `scripts/backup_cron.sh` nightly if the script is present in your repo. It:

- Runs every day at 2 AM.
- Dumps Postgres to `luran_backup_YYYYMMDD_HHMMSS.sql.gz` under `$DEPLOY_PATH/backups/`.
- Deletes backups older than 30 days.
- Logs to `backups/backup.log`.
- Optionally uploads to Google Drive / S3 if `ENABLE_CLOUD_BACKUP=true` in `.env`.

Verify on the VPS:

```bash
crontab -l | grep backup_cron
tail -50 /opt/timpbills/backups/backup.log
ls -lh /opt/timpbills/backups/*.sql.gz
```

If your project doesn't have a `scripts/backup_cron.sh`, the CD step is a no-op. You can add one later — examples exist in the template's source repo.

## Database access

### Option 1 — Adminer (web GUI)

```bash
docker compose --profile tools up -d adminer
# http://localhost:8080  (System: PostgreSQL, Server: db, creds from .env)
docker compose stop adminer   # stop when done — 128 MB idle
```

### Option 2 — CLI (zero RAM)

```bash
docker exec -it timpbills_db psql -U <POSTGRES_USER> -d <POSTGRES_DB>

# one-shot query:
docker exec -it timpbills_db psql -U <POSTGRES_USER> -d <POSTGRES_DB> \
  -c "SELECT COUNT(*) FROM users;"

# backup:
docker exec -t timpbills_db pg_dump -U <POSTGRES_USER> <POSTGRES_DB> > backup_$(date +%Y%m%d).sql

# restore:
docker exec -i timpbills_db psql -U <POSTGRES_USER> -d <POSTGRES_DB> < backup.sql
```

## Updates

The CD pipeline handles this for you on every push to `main`. For manual updates:

```bash
cd /opt/timpbills
git pull origin main
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml exec api alembic upgrade head
curl http://localhost:8000/api/v1/health
```

## Monitoring

```bash
docker stats                                  # live CPU/RAM per container
docker compose -f docker-compose.prod.yml ps  # service health
docker compose -f docker-compose.prod.yml logs --tail=100 -f api
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| API won't start; password error | `.env` superuser password must be 12+ chars with mixed case/digit/special. |
| Memory > 80% | Reduce `API_WORKERS`. Restart: `docker compose restart api`. |
| Rate limiting not working | Check Redis is up: `docker compose ps redis`. Check keys: `docker exec -it timpbills_redis redis-cli KEYS 'http_*'`. |
| DB connection errors | `docker compose logs db` and confirm `.env` `DATABASE_URL` points at `db:5432`, not `localhost`. |
| Can't reach API from browser | Nginx not proxying, or firewall blocking 80/443. `sudo ufw status`. |

## Quick reference

| Task | Command |
|---|---|
| Start dev (2 workers) | `API_WORKERS=2 docker compose up -d` |
| Start prod (4 workers) | `docker compose -f docker-compose.prod.yml up -d` |
| Stop all | `docker compose down` |
| View API logs | `docker compose logs -f api` |
| Resource usage | `docker stats` |
| DB GUI | `docker compose --profile tools up -d adminer` |
| DB CLI | `docker exec -it timpbills_db psql -U <user> -d <db>` |
| Restart API | `docker compose restart api` |
| Run migrations | `docker compose -f docker-compose.prod.yml exec api alembic upgrade head` |

## What the production stack gives you

- **Resource limits** per service (prevents one container from OOMing the VPS).
- **Health checks** with auto-restart on unhealthy.
- **Log rotation** (10 MB × 3 files per container — prevents disk fill).
- **Redis persistence** (rate limit counters survive restarts).
- **PostgreSQL tuning** for 8 GB hosts.
