# Env Encryption — Timpbills API

How we ship environment files to the VPS without leaking secrets to git.

## The pattern

1. Keep `.env.staging` and `.env.production` in `.gitignore` (plaintext — **never committed**).
2. Encrypt them to `.env.staging.enc` and `.env.production.enc` with AES-256-CBC + PBKDF2 (100k iterations) using `./scripts/env.sh`.
3. Commit the `.enc` files.
4. Store the AES key as the GitHub environment secret `ENV_ENCRYPTION_KEY` (same key for both environments is fine).
5. CD decrypts on the runner, `scp`s the resulting `.env` to the VPS, then the file is deleted from the runner when the job ends.

This gives you:
- Environment config in version control (auditable, rollback-able).
- Secrets not visible to anyone who clones the repo.
- No manual env-editing on the VPS during deploy — CD handles it.

## The env.sh helper

The template ships `scripts/env.sh` — a single-entry-point helper with six subcommands. Run it with no args for help:

```bash
./scripts/env.sh

Usage: ./scripts/env.sh <command> [environment]

Commands:
  encrypt <staging|production>    Encrypt .env.<env> -> .env.<env>.enc
  decrypt <staging|production>    Decrypt .env.<env>.enc -> .env.<env>
  verify  <staging|production>    Verify encrypted file can be decrypted
  rotate  <staging|production>    Re-encrypt with a new key
  diff                            Show variables that differ between envs
  generate-key                    Generate a random encryption key
```

### Key discovery

`env.sh` looks for the encryption key in three places, in order:

1. `$ENV_ENCRYPTION_KEY` env variable.
2. `.env.key` file in the repo root (gitignored).
3. Interactive prompt (fallback).

Most day-to-day use: put the key in `.env.key` once, and every command Just Works.

## First-time setup

```bash
# 1. Generate a fresh 32-byte hex key
./scripts/env.sh generate-key
# → prints a key; copy it

# 2. Save locally (gitignored)
echo '<paste-the-key>' > .env.key

# 3. Create your plaintext .env files for each environment
cp .env.example .env.staging
cp .env.example .env.production
# ... edit them ...

# 4. Encrypt both
./scripts/env.sh encrypt staging
./scripts/env.sh encrypt production

# 5. Commit the .enc files
git add .env.staging.enc .env.production.enc
git commit -m "chore: add encrypted env files"
```

Also add the same key to **GitHub → Settings → Environments → staging and production → secret `ENV_ENCRYPTION_KEY`**. CD uses it to decrypt on the runner.

## Day-to-day ops

### Edit and re-ship an environment

```bash
./scripts/env.sh decrypt staging      # writes .env.staging
vim .env.staging                      # make your changes
./scripts/env.sh encrypt staging      # writes .env.staging.enc
git add .env.staging.enc
git commit -m "chore: update staging env"
```

### Compare environments

See which vars differ between staging and production:

```bash
./scripts/env.sh decrypt staging
./scripts/env.sh decrypt production
./scripts/env.sh diff
```

### Verify an encrypted file is intact

Does NOT write plaintext to disk:

```bash
./scripts/env.sh verify staging
```

### Rotate the key

```bash
./scripts/env.sh rotate staging
# → prints new key + instructions to re-encrypt the other env
```

Follow the printed instructions exactly — update the GitHub secret, your password manager, and re-encrypt the *other* environment with the new key.

## Manual decrypt (for debugging only)

```bash
openssl enc -aes-256-cbc -d -pbkdf2 -iter 100000 \
  -in .env.staging.enc \
  -out .env.staging \
  -pass "pass:$ENV_ENCRYPTION_KEY"
```

This is what CD does under the hood. `env.sh decrypt` wraps this with key discovery and overwrite protection.

## `.gitignore` must contain

The template's `.gitignore` already has these, but confirm:

```
# Plaintext env files — never commit
.env
.env.local
.env.staging
.env.production
.env.key
```

## Key management

Generate a strong key: `openssl rand -hex 32` (or just `./scripts/env.sh generate-key`).

Store it in **exactly three** places:

1. Your password manager (for recovery).
2. GitHub → Settings → Environments → `staging` / `production` → secret `ENV_ENCRYPTION_KEY`.
3. Your local `.env.key` (gitignored).

**Not in Slack, not in email, not in a plaintext `passphrase.txt` file.**

## Why AES-256-CBC + PBKDF2 and not something fancier?

- It's in **every OpenSSL install on the planet** — zero dependencies on the runner, the VPS, or contributors' laptops.
- PBKDF2 with 100k iterations is fine for keys with ≥128 bits of entropy (a hex-32 key has 256 bits).
- We're not trying to resist a state-level adversary with repo read access — we're trying to make `git clone` insufficient to recover the secrets. AES-256 does that.

If you need per-user access control (different devs see different secrets), switch to `sops` + AWS KMS / age, and swap out `scripts/env.sh` and the decrypt step in `cd.yml`. The rest of the pipeline doesn't care how the `.env` comes into existence — only that it exists at `$DEPLOY_PATH/.env` on the VPS before `docker compose up`.
