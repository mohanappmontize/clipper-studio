#!/usr/bin/env bash
# Start Clipper Studio and open it in the browser. Run ./install.sh once first.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Not installed yet - running ./install.sh first."; ./install.sh || exit 1
fi
exec .venv/bin/python ui/server.py --open "$@"
