#!/usr/bin/env bash
# LLM Observability Stack — Deployment Script
# Deploys to server (localhost)
#
# Components:
#   1. Langfuse v3 (web + worker + Postgres + ClickHouse + Redis + MinIO)
#      - Postgres on port 54332 (Langfuse DB + monitoring DB)
#      - Web UI on port 3300
#   2. Promptfoo (port 3200)
#
# Prerequisites:
#   - sshpass installed locally
#   - SERVER_PASS env var set
#
# Usage: ./deploy.sh [langfuse|promptfoo|schema|status|all]

set -euo pipefail

SERVER="localhost"
SERVER_USER="user"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Credentials sourced from environment — set before running
: "${SERVER_PASS:?Set SERVER_PASS before running deploy.sh}"

# SSH helper
run_remote() {
    sshpass -p "${SERVER_PASS}" ssh -o StrictHostKeyChecking=no "${SERVER_USER}@${SERVER}" "$@"
}

# SCP helper
copy_to() {
    local src="$1"
    local dst="$2"
    sshpass -p "${SERVER_PASS}" scp -o StrictHostKeyChecking=no -r "$src" "${SERVER_USER}@${SERVER}:${dst}"
}

deploy_langfuse() {
    echo "=== Deploying Langfuse v3 (includes Postgres, ClickHouse, Redis, MinIO) ==="

    # Create directory on server
    run_remote "mkdir -p ~/llm-olly-langfuse"

    # Copy compose file and init script
    copy_to "${SCRIPT_DIR}/langfuse/docker-compose.yml" "~/llm-olly-langfuse/docker-compose.yml"
    copy_to "${SCRIPT_DIR}/langfuse/init-monitoring-db.sh" "~/llm-olly-langfuse/init-monitoring-db.sh"

    # Generate .env if it doesn't exist
    run_remote '
        if [ ! -f ~/llm-olly-langfuse/.env ]; then
            echo "Generating secrets..."
            PG_PASS=$(openssl rand -base64 16 | tr -d "=+/")
            NEXTAUTH=$(openssl rand -base64 32)
            SALT_VAL=$(openssl rand -base64 32)
            ENC_KEY=$(openssl rand -hex 32)
            CH_PASS=$(openssl rand -base64 16 | tr -d "=+/")
            MINIO_PASS=$(openssl rand -base64 16 | tr -d "=+/")
            REDIS_PASS=$(openssl rand -base64 16 | tr -d "=+/")
            cat > ~/llm-olly-langfuse/.env << EOF
POSTGRES_PASSWORD=${PG_PASS}
NEXTAUTH_SECRET=${NEXTAUTH}
SALT=${SALT_VAL}
ENCRYPTION_KEY=${ENC_KEY}
CLICKHOUSE_USER=clickhouse
CLICKHOUSE_PASSWORD=${CH_PASS}
MINIO_USER=minio
MINIO_PASSWORD=${MINIO_PASS}
REDIS_AUTH=${REDIS_PASS}
EOF
            echo "Generated ~/llm-olly-langfuse/.env"
        else
            echo ".env already exists, skipping generation"
        fi
    '

    # Start Langfuse stack
    run_remote "cd ~/llm-olly-langfuse && docker compose up -d"

    echo "=== Langfuse v3 deploying ==="
    echo "  Web UI:   http://${SERVER}:3300"
    echo "  Postgres: postgresql://langfuse:***@${SERVER}:54332/langfuse"
    echo "  Monitor:  postgresql://langfuse:***@${SERVER}:54332/monitoring"
}

deploy_schema() {
    echo "=== Applying monitoring schema to 'monitoring' database ==="

    # Wait for Postgres to be ready
    for i in $(seq 1 10); do
        if run_remote "docker exec llm-olly-postgres pg_isready -U langfuse -d monitoring 2>/dev/null"; then
            break
        fi
        echo "Waiting for Postgres... ($i/10)"
        sleep 2
    done

    # Copy and apply schema to the 'monitoring' database
    copy_to "${SCRIPT_DIR}/schema/001_monitoring_tables.sql" "/tmp/001_monitoring_tables.sql"
    run_remote "docker exec -i llm-olly-postgres psql -U langfuse -d monitoring < /tmp/001_monitoring_tables.sql"

    echo "=== Schema applied to 'monitoring' database ==="
}

deploy_promptfoo() {
    echo "=== Deploying Promptfoo ==="

    # Create directory
    run_remote "mkdir -p ~/llm-olly-promptfoo/configs"

    # Copy compose file
    copy_to "${SCRIPT_DIR}/promptfoo/docker-compose.yml" "~/llm-olly-promptfoo/docker-compose.yml"

    # Create .env if it doesn't exist
    run_remote '
        if [ ! -f ~/llm-olly-promptfoo/.env ]; then
            cat > ~/llm-olly-promptfoo/.env << EOF
ANTHROPIC_API_KEY=FILL_ME
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
EOF
            echo "Generated ~/llm-olly-promptfoo/.env — fill in ANTHROPIC_API_KEY"
        else
            echo ".env already exists, skipping"
        fi
    '

    # Ensure llm-olly network exists (created by Langfuse compose)
    run_remote "docker network inspect llm-olly >/dev/null 2>&1 || docker network create llm-olly"

    # Start Promptfoo
    run_remote "cd ~/llm-olly-promptfoo && docker compose up -d"

    echo "=== Promptfoo ready ==="
    echo "  Web UI: http://${SERVER}:3200"
}

status() {
    echo "=== LLM Observability Stack Status ==="
    run_remote "docker ps --filter 'name=llm-olly' --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
}

case "${1:-}" in
    langfuse)  deploy_langfuse ;;
    schema)    deploy_schema ;;
    promptfoo) deploy_promptfoo ;;
    status)    status ;;
    all)
        deploy_langfuse
        echo "Waiting 15s for Postgres init..."
        sleep 15
        deploy_schema
        deploy_promptfoo
        echo ""
        status
        ;;
    *)
        echo "Usage: $0 [langfuse|schema|promptfoo|status|all]"
        echo ""
        echo "Deploy order: langfuse (includes Postgres) -> schema -> promptfoo"
        exit 1
        ;;
esac
