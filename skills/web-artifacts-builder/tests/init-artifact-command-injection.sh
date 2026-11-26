#!/usr/bin/env bash
# Spec for the project-name command injection in scripts/init-artifact.sh.
#
# These tests fail while the vulnerability is present. A safe project name
# is letters, digits, and hyphens. Any other name must be refused before
# the script prints it, creates a file, or passes it to sed. Refusing after
# pnpm create, or editing index.html with the original name still inside a
# sed expression, does not pass.
#
# Run:
#   bash skills/web-artifacts-builder/tests/init-artifact-command-injection.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/init-artifact.sh"
SAFE_NAME="safe-slug-1"

failures=0
cleanup_paths=()

cleanup() {
  local path
  for path in "${cleanup_paths[@]}"; do
    rm -rf "$path"
  done
}
trap cleanup EXIT

fail() {
  local name="$1"
  local detail="$2"
  printf 'FAIL %s: %s\n' "$name" "$detail"
  failures=$((failures + 1))
}

pass() {
  printf 'PASS %s\n' "$1"
}

# $1 test name, $2 project name, $3 sentinel path that the payload would create
expect_rejected() {
  local name="$1"
  local project_name="$2"
  local sentinel="$3"
  local work log ec detail
  work="$(mktemp -d)"
  log="$(mktemp)"
  cleanup_paths+=("$work" "$log" "$sentinel")
  rm -f "$sentinel"

  set +e
  (
    cd "$work" || exit 97
    timeout 180 bash "$SCRIPT" "$project_name"
  ) >"$log" 2>&1
  ec=$?
  set -e

  detail=""
  if [[ -e "$sentinel" ]]; then
    detail+="sentinel created, so a shell command ran; "
  fi
  if [[ -n "$(find "$work" -mindepth 1 -print -quit)" ]]; then
    detail+="working directory gained files before refusal; "
  fi
  if [[ "$ec" -eq 0 ]]; then
    detail+="script exited 0; "
  fi
  if [[ "$ec" -eq 124 ]]; then
    detail+="script timed out; "
  fi
  if grep -q "Creating new React + Vite project:" "$log"; then
    detail+="project name was expanded in the status line before refusal; "
  fi
  if [[ -n "$detail" ]]; then
    fail "$name" "$detail"
    printf '  exit=%s log_head:\n' "$ec"
    sed -n '1,20p' "$log" | sed 's/^/  | /'
  else
    pass "$name"
  fi
}

expect_safe_name() {
  local name="safe project name still initializes"
  local work log ec
  work="$(mktemp -d)"
  log="$(mktemp)"
  cleanup_paths+=("$work" "$log")

  set +e
  (
    cd "$work" || exit 97
    timeout 180 bash "$SCRIPT" "$SAFE_NAME"
  ) >"$log" 2>&1
  ec=$?
  set -e

  local detail=""
  if [[ "$ec" -ne 0 ]]; then
    detail+="exit ${ec}; "
  fi
  if [[ ! -f "$work/$SAFE_NAME/package.json" ]]; then
    detail+="package.json was not created; "
  fi
  if [[ -f "$work/$SAFE_NAME/index.html" ]] &&
    ! grep -q "<title>${SAFE_NAME}</title>" "$work/$SAFE_NAME/index.html"; then
    detail+="index.html title was not set to the project name; "
  fi
  if [[ ! -f "$work/$SAFE_NAME/index.html" ]]; then
    detail+="index.html missing; "
  fi
  if [[ -n "$detail" ]]; then
    fail "$name" "$detail"
    printf '  log_head:\n'
    sed -n '1,20p' "$log" | sed 's/^/  | /'
  else
    pass "$name"
  fi
}

if [[ ! -f "$SCRIPT" ]]; then
  echo "missing $SCRIPT" >&2
  exit 2
fi

sentinel_root="$(mktemp -d)"
cleanup_paths+=("$sentinel_root")

# GNU sed treats an unescaped "/" as the end of the s command and then
# compiles the rest of the project name as further sed commands. The e
# command and the s///e flag both popen() a shell.
expect_rejected \
  "sed e command in project name" \
  "x/;e touch ${sentinel_root}/e-command;#" \
  "${sentinel_root}/e-command"

expect_rejected \
  "sed substitution e flag in project name" \
  "x/;s@^@touch ${sentinel_root}/s-flag; #@e;#" \
  "${sentinel_root}/s-flag"

# The shell does not rescan these inside double quotes. They are still
# unsafe names and must be refused before the script creates a project.
expect_rejected \
  "command substitution text in project name" \
  '$(touch '"${sentinel_root}/subst"')' \
  "${sentinel_root}/subst"

expect_rejected \
  "backtick command text in project name" \
  '`touch '"${sentinel_root}/backtick"'`' \
  "${sentinel_root}/backtick"

expect_safe_name

echo
if [[ "$failures" -eq 0 ]]; then
  echo "ok"
  exit 0
fi
echo "${failures} test(s) failed"
exit 1
