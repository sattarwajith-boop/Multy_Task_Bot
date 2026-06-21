#!/usr/bin/env bash
set -e

cd /usr/src/app

if [ ! -f "bot/__main__.py" ]; then
  echo "FATAL: /usr/src/app/bot/__main__.py is missing."
  echo "Deploy the contents of Unified-Leech-Bot as the repository root."
  find /usr/src/app -maxdepth 2 -type f | sort | head -100
  exit 1
fi

exec python3 -m bot
