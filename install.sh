#!/usr/bin/env bash
# One-click setup on macOS / Linux. Needs Python 3.9+ (python.org, Homebrew or your package manager).
cd "$(dirname "$0")" || exit 1
for py in python3.12 python3.11 python3.13 python3.10 python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    exec "$py" install.py "$@"
  fi
done
echo "Python 3.9 or newer was not found. Install it (https://www.python.org/downloads/) and run ./install.sh again."
exit 1
