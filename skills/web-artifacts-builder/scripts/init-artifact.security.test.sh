#!/usr/bin/env bash
# Regression tests for project-name command injection in init-artifact.sh.
#
# These tests fail while the vulnerability is present. A later fix should
# make them pass by rejecting an unsafe project name before the script
# creates any file and before that name is interpolated into a shell or
# sed program. Sanitizing only the final path segment (create-vite treats
# it as the package name) or stripping only <>:"\|?* is not enough: the
# payloads below keep a valid final segment and avoid that character set,
# and the raw argument is what reaches sed.
#
# Run:
#   bash skills/web-artifacts-builder/scripts/init-artifact.security.test.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SCRIPT_DIR/init-artifact.sh"
FAILURES=0
# 0.25s * 240 = 60s. Scaffolding a vulnerable name finishes well inside this;
# a fixed script returns immediately.
POLLS=240

if [[ ! -f "$SCRIPT" ]]; then
  echo "FAIL: init-artifact.sh not found at $SCRIPT" >&2
  exit 1
fi

kill_tree() {
  local pid="$1"
  local child
  for child in $(ps -o pid= --ppid "$pid" 2>/dev/null); do
    kill_tree "$child"
  done
  kill -KILL "$pid" 2>/dev/null || true
}

# $1 label
# $2 project name; the literal token __SENTINEL__ is replaced with a fresh
# absolute path that the name must not create.
expect_rejected() {
  local label="$1"
  local name="$2"
  local root parent sentinel log
  root="$(mktemp -d)"
  parent="$root/parent"
  sentinel="$root/sentinel"
  log="$root/log"
  mkdir -p "$parent"
  name="${name//__SENTINEL__/$sentinel}"

  local pid status=0 timed_out=0 saw_sentinel=0
  (
    cd "$parent" || exit 99
    bash "$SCRIPT" "$name" </dev/null >"$log" 2>&1
  ) &
  pid=$!

  local i
  for i in $(seq 1 "$POLLS"); do
    if [[ -e "$sentinel" ]]; then
      saw_sentinel=1
      kill_tree "$pid"
      wait "$pid" 2>/dev/null || true
      break
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid"
      status=$?
      break
    fi
    sleep 0.25
  done

  if kill -0 "$pid" 2>/dev/null; then
    timed_out=1
    kill_tree "$pid"
    wait "$pid" 2>/dev/null || true
    status=124
  fi

  local created
  created="$(find "$parent" -mindepth 1 -print 2>/dev/null || true)"

  local ok=1
  if [[ "$saw_sentinel" -eq 1 || -e "$sentinel" ]]; then
    echo "FAIL: $label: project name executed a shell command"
    ok=0
  fi
  if [[ -n "$created" ]]; then
    echo "FAIL: $label: script created files before refusing the project name"
    ok=0
  fi
  if [[ "$timed_out" -eq 1 && "$saw_sentinel" -eq 0 ]]; then
    echo "FAIL: $label: script kept running instead of refusing the project name"
    ok=0
  fi
  if [[ "$status" -eq 0 && "$saw_sentinel" -eq 0 && "$timed_out" -eq 0 ]]; then
    echo "FAIL: $label: script exited 0 for an unsafe project name"
    ok=0
  fi
  if [[ "$ok" -eq 1 ]]; then
    echo "PASS: $label"
  else
    FAILURES=$((FAILURES + 1))
    echo "----- log ($label) -----"
    if [[ -f "$log" ]]; then
      tail -n 40 "$log"
    else
      echo "(no log)"
    fi
  fi

  rm -rf "$root"
}

# Slash ends the sed substitute command, ';' starts a new command, and GNU
# sed's e command passes the rest of the line to the shell. The final
# segment is the valid package name "app". ${IFS} stays literal until sed's
# shell expands it, so the name contains no space and none of <>:"\|?*.
expect_rejected \
  "sed e command runs a shell command from the project name" \
  'x/;e/bin/touch${IFS}__SENTINEL__;#/app'

# Same breakout with an ordinary space. The argument is quoted, so the
# space is part of the project name rather than a shell metacharacter of
# the caller.
expect_rejected \
  "sed e command runs a shell command when the project name contains a space" \
  'x/;e/bin/touch __SENTINEL__;#/app'

# The published example uses '|' and '>'. create-vite deletes those
# characters before mkdir, so the original string may not be the directory
# that is entered. Refusal still has to happen first: scaffolding that
# stripped name, or executing the command if the characters survive, is a
# failure.
expect_rejected \
  "pipe and redirection in the project name are refused before any scaffold" \
  'x/;e>__SENTINEL__|true;#/app'

# The same sed program is the sink for every sed command, not only e.
# 'w' is compiled from the text after the slash.
expect_rejected \
  "sed w command in the project name is refused before any scaffold" \
  'x/;w__SENTINEL__'

echo
if [[ "$FAILURES" -ne 0 ]]; then
  echo "$FAILURES case(s) failed"
  exit 1
fi
echo "all cases passed"
exit 0
