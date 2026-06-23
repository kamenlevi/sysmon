#!/usr/bin/env bash
# Run directly from source (no install required)
cd "$(dirname "$0")"
PYTHONPATH=. exec python3 -m baro "$@"
