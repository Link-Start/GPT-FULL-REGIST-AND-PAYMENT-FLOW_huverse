#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

name="gpt-trial-protocol-$(date +%Y%m%d-%H%M%S)"
mkdir -p dist

tar \
  --exclude='./.venv' \
  --exclude='./runtime' \
  --exclude='./dist' \
  --exclude='./.pytest_cache' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  -czf "dist/${name}.tar.gz" \
  --transform "s#^\\.#${name}#" \
  .

echo "created dist/${name}.tar.gz"
