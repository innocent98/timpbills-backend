# Database Backups — Timpbills API

This project ships with a four-script backup/restore system under `scripts/`. It supports local gzipped `pg_dump` snapshots, optional cloud uploads (S3, Google Drive, Dropbox), and one-command restore with automatic pre-restore safety snapshots.

## Which script does what?

| Script | Audience | Where it runs | Notes |
|---|---|---|---|
| `scripts/backup_db.sh` | Developers — manual/ad-hoc backups | Laptop / dev machine | Sources `.env`, colored output, fails fast. |
| `scripts/backup_cron.sh` | Production — unattended daily backups | VPS host (via cron) | Reads `.env` via `grep`, absolute paths, ISO-8601 log timestamps. Safer for cron. |
| `scripts/backup_status.sh` | Operators — health check | Either environment | Shows cron status, local + cloud counts, last log entries. |
| `scripts/restore_db.sh` | Both | Wherever the DB container runs | Creates a safety backup *before* restoring. Requires `yes` typed literally. |

> The production cron is installed **automatically** by the CD pipeline (`.github/workflows/cd.yml`) on every deploy. You don't need to touch cron manually on the VPS — see `CICD_PIPELINE.md`.

## Quick start

### Create a local backup (dev)

```bash
./scripts/backup_db.sh
```

Output: `./backups/timpbills_backup_YYYYMMDD_HHMMSS.sql.gz`. Old backups (> 30 days) auto-pruned.

### Check backup health

```bash
./scripts/backup_status.sh
```

### Restore from a backup

```bash
./scripts/restore_db.sh ./backups/timpbills_backup_20260422_020000.sql.gz
```

You'll be prompted for `yes`. A pre-restore safety snapshot is always taken first.

## What gets backed up

A single `pg_dump` of the database named by `POSTGRES_DB` in your `.env`, via the Postgres container named `${COMPOSE_PROJECT_NAME}_db`. That's it — not files on disk, not Redis, not Qdrant. If you need those, extend the scripts.

## Automated backups in production

The CD pipeline installs this cron idempotently on every deploy:

```cron
0 2 * * * /opt/timpbills/scripts/backup_cron.sh >> /opt/timpbills/backups/backup.log 2>&1
```

Translation: 2 AM local time every day. Logs to `/opt/timpbills/backups/backup.log`.

Verify on the VPS:

```bash
ssh <vps>
crontab -l | grep backup_cron
tail -50 /opt/timpbills/backups/backup.log
ls -lh /opt/timpbills/backups/
```

## Cloud uploads (optional)

All three scripts honor the same `.env` flags. Set them once, they apply everywhere.

### Google Drive (via rclone)

```bash
# 1. Install rclone locally OR on the VPS:
brew install rclone          # macOS
sudo apt install rclone      # Linux

# 2. Configure once — opens a browser for OAuth:
rclone config
# Choose "n" (new remote), name it "gdrive", pick "Google Drive".

# 3. Add to .env:
ENABLE_CLOUD_BACKUP=true
CLOUD_PROVIDER=gdrive
GDRIVE_FOLDER=timpbills-backups

# 4. Test:
./scripts/backup_db.sh
rclone ls gdrive:timpbills-backups/
```

### AWS S3

```bash
# 1. Install awscli:
brew install awscli          # macOS
sudo apt install awscli      # Linux

# 2. Configure credentials:
aws configure

# 3. Create the bucket:
aws s3 mb s3://timpbills-backups

# 4. Add to .env:
ENABLE_CLOUD_BACKUP=true
CLOUD_PROVIDER=s3
AWS_S3_BUCKET=timpbills-backups

# 5. Test:
./scripts/backup_db.sh
aws s3 ls s3://timpbills-backups/backups/
```

### Dropbox (via rclone)

Same as Google Drive, but choose "Dropbox" in `rclone config` and set `CLOUD_PROVIDER=dropbox` + `DROPBOX_FOLDER=timpbills-backups`.

### Backblaze B2 (via rclone)

Backblaze B2 is the cheapest durable object storage option out there — roughly 1/4 the price of S3. Same rclone pattern:

```bash
# 1. Install rclone (same as above)
brew install rclone        # macOS
sudo apt install rclone    # Linux

# 2. Create a B2 application key in the Backblaze console:
#    Backblaze → Account → Application Keys → "Add a New Application Key"
#    Write down the keyID and applicationKey.

# 3. Configure rclone for B2:
rclone config
# Choose "n" (new remote)
# Name it: b2
# Storage: Backblaze B2
# Paste keyID and applicationKey when prompted.

# 4. Create a B2 bucket (in the console or via rclone):
rclone mkdir b2:timpbills-backups

# 5. Add to .env.<env> (and re-encrypt with ./scripts/env.sh encrypt):
ENABLE_CLOUD_BACKUP=true
CLOUD_PROVIDER=b2
B2_BUCKET=timpbills-backups
# Optional — override if your rclone remote isn't named "b2":
# B2_RCLONE_REMOTE=backblaze

# 6. Test:
./scripts/backup_db.sh
rclone ls b2:timpbills-backups/
```

**Why B2 over S3?** Pricing — B2 is ~$6/TB/month vs S3 Standard at ~$23/TB/month, with no egress fees to Cloudflare (via their Bandwidth Alliance). For disaster-recovery backups that you hope never to download, it's the obvious default unless you're already on AWS.

**Lifecycle + retention.** Backblaze's web console lets you set a bucket-level lifecycle policy (e.g., "hide files after 90 days, delete after 120"). That's an additional layer on top of the script's local `RETENTION_DAYS` — use both.

### Using B2's S3-compatible API instead (advanced)

If you prefer the `aws` CLI over `rclone`, Backblaze offers S3-compatible endpoints. Point `aws` at the B2 endpoint:

```bash
aws s3 cp file.gz s3://bucket/path/ \
  --endpoint-url https://s3.us-west-000.backblazeb2.com
```

Add `--endpoint-url` handling to the `s3` branch of `backup_cron.sh` if you go this route. The `b2` provider in the script is rclone-only and simpler — stick with it unless you have a reason.

## Emergency recovery from cloud

Primary DB is corrupted. You're SSH'd into the VPS. Walk-through:

```bash
# 1. Download the latest cloud backups into the local backups dir
#    Examples for each provider:
rclone copy gdrive:timpbills-backups/  /opt/timpbills/backups/ --include "*.sql.gz" --max-age 7d
rclone copy dropbox:timpbills-backups/ /opt/timpbills/backups/ --include "*.sql.gz" --max-age 7d
rclone copy b2:timpbills-backups/      /opt/timpbills/backups/ --include "*.sql.gz" --max-age 7d
aws s3 sync s3://timpbills-backups/   /opt/timpbills/backups/ --exclude "*" --include "*.sql.gz"

# 2. Find the most recent one
cd /opt/timpbills
ls -lt backups/timpbills_backup_*.sql.gz | head -5

# 3. Restore (will prompt for confirmation)
./scripts/restore_db.sh backups/timpbills_backup_YYYYMMDD_HHMMSS.sql.gz

# 4. Verify
docker compose -f docker-compose.prod.yml exec db \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"
```

## Retention + schedule recommendations

| Environment | Frequency | Local retention | Cloud |
|---|---|---|---|
| Development | On demand | 30 days | Optional |
| Staging | Daily | 60 days | Recommended |
| Production | Daily (or every 6h for high-write systems) | 30 days locally, 90 days cloud | Required |

Change the retention window by editing `RETENTION_DAYS` at the top of `backup_cron.sh` / `backup_db.sh`.

## Test your restore path monthly

The worst time to find out your backups don't restore is during a real outage. Pick a date each month:

```bash
# 1. Spin up a throwaway Postgres container
docker run --rm -d --name restore-test \
  -e POSTGRES_PASSWORD=test \
  -e POSTGRES_USER=test \
  -e POSTGRES_DB=test \
  -p 55432:5432 postgres:15-alpine

# 2. Gunzip + pipe the backup in directly
gunzip -c backups/timpbills_backup_<latest>.sql.gz | \
  docker exec -i restore-test psql -U test -d test >/dev/null

# 3. Spot-check a critical table
docker exec restore-test psql -U test -d test -c "SELECT COUNT(*) FROM users;"

# 4. Tear down
docker stop restore-test
```

If step 3 comes back with a plausible count, your backup chain is healthy.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Database container 'timpbills_db' is not running` | `docker compose -f docker-compose.prod.yml up -d db` |
| `Permission denied` when running `./scripts/backup_db.sh` | `chmod +x scripts/*.sh` |
| `gzip: stdout: No space left on device` | `df -h`, then delete old backups manually or lower `RETENTION_DAYS`. |
| `rclone: command not found` in cloud upload step | Install rclone on the VPS, run `rclone config`. |
| Cron runs but log empty | Check `crontab -l` points to the right absolute path, and that the `/opt/timpbills/backups/` dir exists + is writable. |

## Why two backup scripts and not one?

`backup_db.sh` uses `source .env`, which is fast and convenient but fragile: if any value in `.env` contains unescaped quotes, backticks, or `$`, the shell tries to evaluate them and the script may silently execute unintended code (or fail to load the vars at all). That's unacceptable for unattended cron jobs.

`backup_cron.sh` avoids that entirely by reading `.env` as plain data:

```bash
_env_val() { grep "^${1}=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
POSTGRES_USER="$(_env_val POSTGRES_USER)"
```

No shell evaluation happens. Values containing any character stay intact. The script also uses absolute paths everywhere because cron has no `$PWD` context — `cd`-then-run is a common cron bug.

Keep the split. You get the ergonomic dev script AND the safe production script for the price of maintaining ~80 lines of small divergence.
