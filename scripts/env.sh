#!/usr/bin/env bash
# ============================================================
# Timpbills API .env Encryption/Decryption Helper
#
# Manages encrypted .env files per environment:
#   .env.staging      <->  .env.staging.enc
#   .env.production   <->  .env.production.enc
#
# Usage:
#   ./scripts/env.sh encrypt staging        # .env.staging -> .env.staging.enc
#   ./scripts/env.sh encrypt production     # .env.production -> .env.production.enc
#   ./scripts/env.sh decrypt staging        # .env.staging.enc -> .env.staging
#   ./scripts/env.sh decrypt production     # .env.production.enc -> .env.production
#   ./scripts/env.sh verify staging         # check .env.staging.enc is valid
#   ./scripts/env.sh rotate staging         # re-encrypt with a new key
#   ./scripts/env.sh diff                   # show which vars differ between envs
#   ./scripts/env.sh generate-key           # generate a new encryption key
#
# The encryption key is read from (in order):
#   1. ENV_ENCRYPTION_KEY environment variable
#   2. .env.key file (gitignored)
#   3. Interactive prompt
#
# Both environments use the SAME key for simplicity.
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
KEY_FILE="$PROJECT_ROOT/.env.key"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

VALID_ENVS=("staging" "production")

validate_env() {
    local env="$1"
    for valid in "${VALID_ENVS[@]}"; do
        [ "$env" = "$valid" ] && return 0
    done
    echo -e "${RED}Error: Invalid environment '$env'. Use: staging | production${NC}"
    exit 1
}

env_file() { echo "$PROJECT_ROOT/.env.$1"; }
enc_file() { echo "$PROJECT_ROOT/.env.$1.enc"; }

get_key() {
    if [ -n "${ENV_ENCRYPTION_KEY:-}" ]; then
        echo "$ENV_ENCRYPTION_KEY"
        return
    fi
    if [ -f "$KEY_FILE" ]; then
        cat "$KEY_FILE"
        return
    fi
    echo -e "${YELLOW}No ENV_ENCRYPTION_KEY found in environment or .env.key file.${NC}" >&2
    read -rsp "Enter encryption key: " key
    echo >&2
    echo "$key"
}

cmd_encrypt() {
    local env="$1"
    validate_env "$env"
    local plain enc
    plain=$(env_file "$env")
    enc=$(enc_file "$env")

    if [ ! -f "$plain" ]; then
        echo -e "${RED}Error: $plain not found.${NC}"
        echo "Copy your $env .env file here first:"
        echo "  scp -P <PORT> <USER>@<HOST>:<DEPLOY_PATH>/.env $plain"
        exit 1
    fi

    local key
    key=$(get_key)
    [ -z "$key" ] && { echo -e "${RED}Error: No encryption key provided.${NC}"; exit 1; }

    openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
        -in "$plain" -out "$enc" -pass "pass:$key"

    echo -e "${GREEN}Encrypted:${NC} .env.$env -> .env.$env.enc"
    echo -e "  File size: $(wc -c < "$enc") bytes"
    echo -e "  Variables: $(grep -c '=' "$plain" || true)"
    echo ""
    echo -e "${YELLOW}Next steps:${NC}"
    echo "  git add .env.$env.enc"
    echo "  git commit -m 'chore: update encrypted $env env'"
}

cmd_decrypt() {
    local env="$1"
    validate_env "$env"
    local plain enc
    plain=$(env_file "$env")
    enc=$(enc_file "$env")

    if [ ! -f "$enc" ]; then
        echo -e "${RED}Error: $enc not found.${NC}"
        echo "Run 'encrypt $env' first or pull from git."
        exit 1
    fi

    if [ -f "$plain" ]; then
        echo -e "${YELLOW}Warning: .env.$env already exists. Overwrite? (y/N)${NC}"
        read -r confirm
        [ "$confirm" != "y" ] && [ "$confirm" != "Y" ] && { echo "Aborted."; exit 0; }
    fi

    local key
    key=$(get_key)
    [ -z "$key" ] && { echo -e "${RED}Error: No encryption key provided.${NC}"; exit 1; }

    if ! openssl enc -aes-256-cbc -d -pbkdf2 -iter 100000 \
        -in "$enc" -out "$plain" -pass "pass:$key" 2>/dev/null; then
        rm -f "$plain"
        echo -e "${RED}Decryption failed. Wrong key or corrupted file.${NC}"
        exit 1
    fi

    echo -e "${GREEN}Decrypted:${NC} .env.$env.enc -> .env.$env"
    echo -e "  Variables: $(grep -c '=' "$plain" || true)"
}

cmd_verify() {
    local env="$1"
    validate_env "$env"
    local enc
    enc=$(enc_file "$env")

    [ ! -f "$enc" ] && { echo -e "${RED}Error: $enc not found.${NC}"; exit 1; }

    local key
    key=$(get_key)

    # Decrypt into a variable rather than piping to `head`: under `pipefail`
    # an early-exiting reader SIGPIPEs openssl, and the pipeline then reports
    # failure for a perfectly good file and key.
    local decrypted
    decrypted=$(openssl enc -aes-256-cbc -d -pbkdf2 -iter 100000 \
        -in "$enc" -pass "pass:$key" 2>/dev/null || true)

    if printf '%s' "$decrypted" | grep -q '='; then
        echo -e "${GREEN}Verification passed.${NC} .env.$env.enc can be decrypted."
    else
        echo -e "${RED}Verification failed.${NC} Wrong key or corrupted file."
        exit 1
    fi
}

cmd_rotate() {
    local env="$1"
    validate_env "$env"
    local enc
    enc=$(enc_file "$env")

    [ ! -f "$enc" ] && { echo -e "${RED}Error: $enc not found.${NC}"; exit 1; }

    echo -e "${YELLOW}Rotating key for $env...${NC}"

    local old_key
    old_key=$(get_key)

    local tmp="$PROJECT_ROOT/.env.$env.tmp"
    if ! openssl enc -aes-256-cbc -d -pbkdf2 -iter 100000 \
        -in "$enc" -out "$tmp" -pass "pass:$old_key" 2>/dev/null; then
        rm -f "$tmp"
        echo -e "${RED}Decryption failed with current key.${NC}"
        exit 1
    fi

    local new_key
    new_key=$(openssl rand -hex 32)

    openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
        -in "$tmp" -out "$enc" -pass "pass:$new_key"
    rm -f "$tmp"

    echo -e "${GREEN}New key:${NC} $new_key"
    echo ""
    echo -e "${YELLOW}IMPORTANT - Update ALL locations with the new key:${NC}"
    echo "  1. GitHub Actions secret: ENV_ENCRYPTION_KEY (in both staging and production environments)"
    echo "  2. Your password manager"
    echo ""
    echo "  Then re-encrypt the OTHER environment too:"
    local other
    [ "$env" = "staging" ] && other="production" || other="staging"
    echo "  ./scripts/env.sh decrypt $other"
    echo "  ENV_ENCRYPTION_KEY='$new_key' ./scripts/env.sh encrypt $other"
}

cmd_diff() {
    local staging production
    staging=$(env_file "staging")
    production=$(env_file "production")

    if [ ! -f "$staging" ] || [ ! -f "$production" ]; then
        echo -e "${YELLOW}Decrypt both environments first:${NC}"
        [ ! -f "$staging" ] && echo "  ./scripts/env.sh decrypt staging"
        [ ! -f "$production" ] && echo "  ./scripts/env.sh decrypt production"
        exit 1
    fi

    echo -e "${CYAN}Variables that DIFFER between staging and production:${NC}"
    echo ""

    local any_diff=false
    while IFS='=' read -r key value; do
        [ -z "$key" ] && continue
        [[ "$key" =~ ^# ]] && continue
        key=$(echo "$key" | xargs)

        local staging_val production_val
        staging_val=$(grep "^${key}=" "$staging" 2>/dev/null | head -1 | cut -d= -f2-)
        production_val=$(grep "^${key}=" "$production" 2>/dev/null | head -1 | cut -d= -f2-)

        if [ "$staging_val" != "$production_val" ]; then
            echo -e "  ${YELLOW}$key${NC}"
            echo "    staging:    ${staging_val:-(not set)}"
            echo "    production: ${production_val:-(not set)}"
            echo ""
            any_diff=true
        fi
    done < <(cat "$staging" "$production" | grep '=' | sort -u -t= -k1,1)

    if [ "$any_diff" = false ]; then
        echo -e "  ${GREEN}All variables are identical.${NC}"
    fi
}

cmd_generate_key() {
    local key
    key=$(openssl rand -hex 32)
    echo -e "${GREEN}Generated key:${NC} $key"
    echo ""
    echo "Save this key to:"
    echo "  1. GitHub Actions secret -> ENV_ENCRYPTION_KEY (in staging and production environments)"
    echo "  2. Your password manager"
    echo ""
    echo "To save locally (gitignored):"
    echo "  echo '$key' > .env.key"
}

# ---- Main ----

case "${1:-help}" in
    encrypt)
        [ -z "${2:-}" ] && { echo -e "${RED}Usage: ./scripts/env.sh encrypt <staging|production>${NC}"; exit 1; }
        cmd_encrypt "$2" ;;
    decrypt)
        [ -z "${2:-}" ] && { echo -e "${RED}Usage: ./scripts/env.sh decrypt <staging|production>${NC}"; exit 1; }
        cmd_decrypt "$2" ;;
    verify)
        [ -z "${2:-}" ] && { echo -e "${RED}Usage: ./scripts/env.sh verify <staging|production>${NC}"; exit 1; }
        cmd_verify "$2" ;;
    rotate)
        [ -z "${2:-}" ] && { echo -e "${RED}Usage: ./scripts/env.sh rotate <staging|production>${NC}"; exit 1; }
        cmd_rotate "$2" ;;
    diff) cmd_diff ;;
    generate-key) cmd_generate_key ;;
    help|*)
        echo "Usage: ./scripts/env.sh <command> [environment]"
        echo ""
        echo "Commands:"
        echo "  encrypt <staging|production>    Encrypt .env.<env> -> .env.<env>.enc"
        echo "  decrypt <staging|production>    Decrypt .env.<env>.enc -> .env.<env>"
        echo "  verify  <staging|production>    Verify encrypted file can be decrypted"
        echo "  rotate  <staging|production>    Re-encrypt with a new key"
        echo "  diff                            Show variables that differ between envs"
        echo "  generate-key                    Generate a random encryption key"
        echo ""
        echo "Files:"
        echo "  .env.staging.enc      Encrypted staging secrets (safe in git)"
        echo "  .env.production.enc   Encrypted production secrets (safe in git)"
        echo "  .env.staging          Decrypted staging (gitignored)"
        echo "  .env.production       Decrypted production (gitignored)"
        echo "  .env.key              Local encryption key (gitignored)"
        echo ""
        echo "The encryption key is read from (in order):"
        echo "  1. ENV_ENCRYPTION_KEY environment variable"
        echo "  2. .env.key file"
        echo "  3. Interactive prompt"
        ;;
esac
