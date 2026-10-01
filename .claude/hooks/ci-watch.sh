#!/usr/bin/env bash
# Claude Code PostToolUse hook (matcher: Bash, asyncRewake) — after a successful `gh pr create`,
# wait in the background for that PR's CI to finish, then wake Claude with the result.
#
# Why a hook: "check CI after opening a PR" in CLAUDE.md is advisory and was skipped in practice
# (a PR was opened on top of a CI that had been red on main for days). A hook runs regardless.
#
# Contract:
#   - exits 0 immediately and silently unless the Bash call was a `gh pr create` that printed a
#     PR URL, so every other Bash call pays only a python3 start-up;
#   - otherwise waits for the checks (`gh pr checks --watch`), then exits 2 with a one-screen
#     summary on stderr, which asyncRewake delivers to the model: either "CI passed" or the
#     failed jobs, each labelled "also red on main" (pre-existing) or "NOT red on main" (look here);
#   - FAILS OPEN (exit 0, no wake) on anything environmental: no gh / not authenticated / no
#     network / checks never appeared / still pending. Silence is therefore NOT proof of green;
#     CLAUDE.md says to confirm with `gh pr checks <n>` when no report arrives.
#
# Opt out with CLAUDE_CI_WATCH=0. Parsed with python3 (no jq); gh's built-in --jq does the rest.
set -u

[ "${CLAUDE_CI_WATCH:-1}" = "0" ] && exit 0

input=$(cat)

pr=$(printf '%s' "$input" | python3 -c '
import json, re, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
cmd = (d.get("tool_input") or {}).get("command") or ""
if not re.search(r"\bgh\s+pr\s+create\b", cmd):
    sys.exit(0)
resp = json.dumps(d.get("tool_response") or "")
found = re.findall(r"github\.com/[^/\s\"\\]+/[^/\s\"\\]+/pull/(\d+)", resp)
print(found[-1] if found else "")
' 2>/dev/null) || exit 0

[ -n "$pr" ] || exit 0
command -v gh >/dev/null 2>&1 || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

# Checks register a few seconds after the PR exists; give them up to ~3 minutes to appear.
tries=0
while [ "$tries" -lt 12 ]; do
  count=$(gh pr checks "$pr" --json name --jq 'length' 2>/dev/null || echo 0)
  [ "${count:-0}" -gt 0 ] && break
  tries=$((tries + 1))
  sleep 15
done
[ "${count:-0}" -gt 0 ] || exit 0

gh pr checks "$pr" --watch --interval 30 >/dev/null 2>&1

checks=$(gh pr checks "$pr" --json name,bucket,link --jq '.[] | "\(.bucket)\t\(.name)\t\(.link)"' 2>/dev/null) || exit 0
[ -n "$checks" ] || exit 0
printf '%s\n' "$checks" | awk -F'\t' '$1=="pending"{f=1} END{exit f?0:1}' && exit 0 # still pending: don't guess

total=$(printf '%s\n' "$checks" | wc -l | tr -d ' ')
failed=$(printf '%s\n' "$checks" | awk -F'\t' '$1=="fail"||$1=="cancel"{print $2 "\t" $3}')

if [ -z "$failed" ]; then
  printf 'CI finished for PR #%s: all %s checks passed. Report this to the user.\n' "$pr" "$total" >&2
  exit 2
fi

# Which jobs were already red on main's latest completed CI run? Those are pre-existing.
main_id=$(gh run list --branch main --workflow ci.yml --limit 10 --json databaseId,conclusion \
  --jq '[.[] | select(.conclusion=="success" or .conclusion=="failure")][0].databaseId // empty' 2>/dev/null)
main_red=""
[ -n "$main_id" ] && main_red=$(gh run view "$main_id" --json jobs \
  --jq '.jobs[] | select(.conclusion=="failure") | .name' 2>/dev/null)

new_lines=""
old_lines=""
while IFS=$'\t' read -r name link; do
  [ -n "$name" ] || continue
  if printf '%s\n' "$main_red" | grep -Fxq -- "$name"; then
    old_lines="${old_lines}  - ${name} (also red on main) ${link}"$'\n'
  else
    new_lines="${new_lines}  - ${name} (NOT red on main) ${link}"$'\n'
  fi
done <<EOF
$failed
EOF

nfail=$(printf '%s\n' "$failed" | grep -c .)
{
  printf 'CI finished for PR #%s: %s of %s checks failed.\n' "$pr" "$nfail" "$total"
  [ -n "$new_lines" ] && printf 'Likely caused by this PR — investigate:\n%s' "$new_lines"
  [ -n "$old_lines" ] && printf 'Pre-existing (main run %s was already red there):\n%s' "${main_id:-?}" "$old_lines"
  printf 'Report this to the user; do not call the work done. Read a failing log with: gh run view --job <id> --log-failed\n'
} >&2
exit 2
