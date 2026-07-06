#!/bin/bash
# Creates the 'monitoring' database alongside Langfuse's 'langfuse' database
# Langfuse gets its own clean public schema in 'langfuse' DB
# Our canary/experiment/edit-tracking tables live in 'monitoring' DB
# Both accessible via the same Postgres instance on port 54332

set -e

echo "Creating monitoring database..."
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE monitoring;
EOSQL

echo "Enabling extensions in monitoring database..."
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "monitoring" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
EOSQL

echo "Monitoring database ready."
