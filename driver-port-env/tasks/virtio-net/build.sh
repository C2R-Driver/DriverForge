#!/bin/sh
set -eu
TASK_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNNER="$TASK_ROOT/.environment/scripts/task-runtime.py"
if [ ! -f "$RUNNER" ]; then
  echo "First export: python3.11 scripts/export-task.py DRIVER DESTINATION" >&2
  exit 2
fi
exec "${C2R_PYTHON:-python3.11}" "$RUNNER" build --task "$TASK_ROOT" "$@"
