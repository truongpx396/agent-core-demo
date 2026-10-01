#!/usr/bin/env bash
# Claude Code PostToolUse hook (matcher: Edit|Write) — run `ruff check` on the Python file
# that was just written, so lint violations surface at edit time instead of in CI's `lint` job.
#
# Contract:
#   - silent and exit 0 when the file is clean (or the hook has nothing to do);
#   - exit 2 with concise findings on stderr when ruff reports violations, which Claude Code
#     feeds back to the model so it can fix them;
#   - FAILS OPEN on anything environmental (no python3, no ruff, file outside the project, ruff
#     itself erroring): a contributor without a venv must never be blocked by this hook.
#
# It only checks; it never fixes. CI (`ruff check .`) remains the real gate — this is the fast
# feedback loop. The hook payload arrives as JSON on stdin; parsed with python3 so the script
# has no jq dependency.
set -u

input=$(cat)

file=$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ti = d.get("tool_input") or {}
tr = d.get("tool_response") or {}
print(ti.get("file_path") or (tr.get("filePath") if isinstance(tr, dict) else "") or "")
' 2>/dev/null) || exit 0

[ -n "$file" ] || exit 0
case "$file" in *.py) ;; *) exit 0 ;; esac

root=${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}
case "$file" in /*) ;; *) file="$root/$file" ;; esac
[ -f "$file" ] || exit 0
case "$file" in "$root"/*) ;; *) exit 0 ;; esac # outside this project: not our lint config

if [ -x "$root/.venv/bin/ruff" ]; then
  ruff="$root/.venv/bin/ruff"
elif command -v ruff >/dev/null 2>&1; then
  ruff=ruff
else
  exit 0
fi

# --force-exclude: honor ruff's exclude list even though the path is passed explicitly.
out=$("$ruff" check --force-exclude --no-fix --output-format=concise "$file" 2>&1)
rc=$?

# ruff: 0 = clean, 1 = violations found, 2 = ruff itself failed (bad config, etc.).
[ "$rc" -eq 1 ] || exit 0

printf 'ruff check failed for %s (CI gate: make lint):\n%s\n' "${file#"$root"/}" "$out" >&2
exit 2
