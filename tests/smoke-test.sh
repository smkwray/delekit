#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 "$ROOT/tools/render_config.py" --check
python3 "$ROOT/tools/verify_kit.py"
python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py'
bash -n "$ROOT"/bin/*.sh "$ROOT"/tests/*.sh
"$ROOT/tests/test-macos-install.sh"
printf 'all smoke tests passed\n'
"$ROOT/tests/test-dairy-runner.sh"
"$ROOT/tests/test-prune-worktrees.sh"
