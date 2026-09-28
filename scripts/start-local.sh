#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo '先创建 .venv 并安装 requirements-lock.txt；详见 README.md。' >&2
  exit 1
fi
exec .venv/bin/python -m app.local_service
