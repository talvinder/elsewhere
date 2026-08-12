#!/bin/sh
# Guard: internal/private content must never enter this PUBLIC repository.
#
# Business strategy, go-to-market, monetization, pricing, competitive analysis,
# partner-integration plans, and candid internal assessments live in the PRIVATE
# companion repo (talvinder/elsewhere-internal). This script fails if any such
# file is about to enter, or already sits in, the public repo.
#
# Modes:
#   --staged    check files staged for commit  (used by the pre-commit hook)
#   --tracked   check every tracked file        (used by pre-push and CI)
set -eu

mode="${1:---staged}"

# Filenames that signal internal content.
name_re='(^|/)(internal|private)/|\.internal\.|(^|/)(BRAND|DISTRIBUTION|GTM|GO-?TO-?MARKET|LAUNCH|STRATEGY|MONETI[SZ]ATION|PRICING|COMPETITIVE|BUSINESS[_-]?MODEL|POSITIONING)[^/]*\.(md|markdown|txt|pdf|docx?|xlsx?|pptx?)$'

# Any file may opt itself out of the public repo by containing this marker.
marker="ELSEWHERE:""INTERNAL"

case "$mode" in
  --tracked) list_command='git ls-files' ;;
  --staged)  list_command='git diff --cached --name-only --diff-filter=ACM' ;;
  *) echo "usage: $0 [--staged|--tracked]" >&2; exit 2 ;;
esac

blocked_file=$(mktemp "${TMPDIR:-/tmp}/elsewhere-public-guard.XXXXXX")
trap 'rm -f "$blocked_file"' EXIT HUP INT TERM

$list_command | while IFS= read -r f; do
  [ -e "$f" ] || continue
  if printf '%s\n' "$f" | grep -Eiq "$name_re"; then
    printf '  %s  — internal filename\n' "$f" >> "$blocked_file"
    continue
  fi
  if [ -f "$f" ] && grep -Iq "$marker" "$f" 2>/dev/null; then
    printf '  %s  — contains %s marker\n' "$f" "$marker" >> "$blocked_file"
  fi
done

if [ -s "$blocked_file" ]; then
  printf '\n\033[1;31mBLOCKED:\033[0m internal content must not go in the PUBLIC repo.\n'
  printf 'Move it to the private repo (talvinder/elsewhere-internal) instead.\n'
  printf 'Offending files:\n'
  cat "$blocked_file"
  printf '\n'
  exit 1
fi
