#!/usr/bin/env bash
# Spec for skills/web-artifacts-builder/scripts/init-artifact.sh
#
# These tests fail while a project name can make the script create, delete, or
# modify files somewhere other than one new single-segment directory under the
# working directory. They pass once the script rejects those names before any
# filesystem change.
#
# A name that path.join keeps under the working directory is still rejected
# when the raw argument is absolute or contains ".." or an extra segment,
# because the script changes directory to the raw argument.
#
# Run from the repository root:
#   bash skills/web-artifacts-builder/tests/init-artifact-path-traversal.test.sh

set -u
export LC_ALL=C

INIT_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/init-artifact.sh"
if [[ ! -f "$INIT_SCRIPT" ]]; then
  echo "FAIL setup: init script not found at $INIT_SCRIPT" >&2
  exit 1
fi

PASS_COUNT=0
FAIL_COUNT=0
KEEP_PIDS=()

kill_descendants() {
  local pid="$1"
  local child
  local children
  children="$(ps -o pid= --ppid "$pid" 2>/dev/null || true)"
  for child in $children; do
    kill_descendants "$child"
  done
  kill -KILL "$pid" 2>/dev/null || true
}

cleanup() {
  local pid
  for pid in "${KEEP_PIDS[@]}"; do
    kill_descendants "$pid"
    wait "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

fail() {
  local name="$1"
  shift
  FAIL_COUNT=$((FAIL_COUNT + 1))
  echo "FAIL $name: $*"
}

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  echo "PASS $1"
}

# snapshot <root> <dest-prefix>
snapshot() {
  local root="$1"
  local dest="$2"
  if [[ ! -d "$root" ]]; then
    : >"$dest.paths"
    : >"$dest.sums"
    return
  fi
  find "$root" -mindepth 1 -printf '%P\n' | LC_ALL=C sort >"$dest.paths"
  : >"$dest.sums"
  local rel
  while IFS= read -r rel; do
    if [[ -f "$root/$rel" && ! -L "$root/$rel" ]]; then
      sha256sum "$root/$rel" | awk -v p="$rel" '{print $1, p}'
    fi
  done <"$dest.paths" | LC_ALL=C sort >"$dest.sums"
}

# Prints paths that were added, removed, or whose contents changed.
diff_snapshots() {
  local before="$1"
  local after="$2"
  comm -3 "$before.paths" "$after.paths" | sed 's/^[[:space:]]*//'
  comm -3 "$before.sums" "$after.sums" | awk '{print $NF}'
}

stop_init() {
  local pid="$1"
  kill_descendants "$pid"
  wait "$pid" 2>/dev/null || true
  local kept=()
  local existing
  for existing in "${KEEP_PIDS[@]}"; do
    if [[ "$existing" != "$pid" ]]; then
      kept+=("$existing")
    fi
  done
  KEEP_PIDS=("${kept[@]}")
}

seed_project() {
  local dir="$1"
  mkdir -p "$dir"
  cat >"$dir/index.html" <<'EOF'
<!doctype html>
<html>
  <head>
    <link rel="icon" type="image/svg+xml" href="/vite.svg" />
    <title>Old Title</title>
  </head>
  <body></body>
</html>
EOF
  printf '%s\n' '{"name":"preseeded","private":true,"version":"0.0.0"}' >"$dir/package.json"
  printf '%s\n' '{"files":[]}' >"$dir/tsconfig.json"
  printf '%s\n' '{"compilerOptions":{}}' >"$dir/tsconfig.app.json"
  printf '%s\n' 'sentinel-do-not-touch' >"$dir/SENTINEL.txt"
}

# reject_in <case> <project-name> <work-dir> <watched-root> <state-dir>
reject_in() {
  local case_name="$1"
  local project_name="$2"
  local work="$3"
  local watched="$4"
  local state="$5"

  local before="$state/before"
  local after="$state/after"
  snapshot "$watched" "$before"

  local log="$state/init.log"
  local pid
  # Start in this shell so the background job is not reaped with a subshell.
  pushd "$work" >/dev/null
  env -u CDPATH bash "$INIT_SCRIPT" "$project_name" </dev/null >"$log" 2>&1 &
  pid=$!
  popd >/dev/null
  KEEP_PIDS+=("$pid")

  local leaked=""
  local status=""
  local i
  for i in $(seq 1 240); do
    snapshot "$watched" "$after"
    leaked="$(diff_snapshots "$before" "$after" || true)"
    if [[ -n "$leaked" ]]; then
      break
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" && status=0 || status=$?
      snapshot "$watched" "$after"
      leaked="$(diff_snapshots "$before" "$after" || true)"
      break
    fi
    sleep 0.25
  done

  if kill -0 "$pid" 2>/dev/null; then
    status="timeout"
  fi
  stop_init "$pid"

  if [[ -n "$leaked" ]]; then
    local sample
    sample="$(printf '%s\n' "$leaked" | head -n 8 | tr '\n' ' ')"
    fail "$case_name" "filesystem changed (name=$(printf %q "$project_name")): $sample"
    echo "    log: $log"
    return
  fi
  if [[ "$status" == "timeout" || -z "$status" ]]; then
    fail "$case_name" "script did not reject $(printf %q "$project_name") before the timeout"
    echo "    log: $log"
    return
  fi
  if [[ "$status" -eq 0 ]]; then
    fail "$case_name" "script exited 0 for $(printf %q "$project_name")"
    echo "    log: $log"
    return
  fi
  pass "$case_name"
}

reject_fresh() {
  local case_name="$1"
  local project_name="$2"
  local sandbox
  sandbox="$(mktemp -d "${TMPDIR:-/tmp}/init-artifact-test.XXXXXX")"
  local watched="$sandbox/tree"
  local state="$sandbox/state"
  mkdir -p "$watched/work" "$state"
  reject_in "$case_name" "$project_name" "$watched/work" "$watched" "$state"
  if [[ "$FAIL_COUNT" -eq 0 ]]; then
    rm -rf "$sandbox"
  fi
}

echo "init script: $INIT_SCRIPT"

reject_fresh "parent-relative" "../outside-app"
reject_fresh "parent-relative-trailing-slash" "../outside-app/"
reject_fresh "nested-parent-segments" "foo/../../outside-app"
reject_fresh "dot-slash-parent" "./../outside-app"
reject_fresh "lexical-parent-inside-cwd" "subdir/../inside-name"
reject_fresh "extra-segment" "nested/child"
reject_fresh "current-directory" "."

# ".." against a directory that already holds a project. create-vite cancels
# on a non-empty target and exits 0; the script then cds at the raw name.
parent_existing() {
  local sandbox
  sandbox="$(mktemp -d "${TMPDIR:-/tmp}/init-artifact-test.XXXXXX")"
  local watched="$sandbox/tree"
  local state="$sandbox/state"
  mkdir -p "$state"
  seed_project "$watched"
  mkdir -p "$watched/work"
  local fails_before="$FAIL_COUNT"
  reject_in "parent-directory-existing-project" ".." "$watched/work" "$watched" "$state"
  if [[ "$FAIL_COUNT" -eq "$fails_before" ]]; then
    rm -rf "$sandbox"
  fi
}
parent_existing

# Absolute names: Node path.join(cwd, absolute) stays under cwd, but
# `cd "$PROJECT_NAME"` uses the absolute path. Watch both.
run_absolute() {
  local case_name="$1"
  local mode="$2"
  local sandbox
  sandbox="$(mktemp -d "${TMPDIR:-/tmp}/init-artifact-test.XXXXXX")"
  local watched="$sandbox/tree"
  local state="$sandbox/state"
  mkdir -p "$watched/work" "$state"
  local target
  if [[ "$mode" == "existing" ]]; then
    target="$watched/abs-target"
    seed_project "$target"
  else
    target="$watched/abs-new"
  fi
  local fails_before="$FAIL_COUNT"
  reject_in "$case_name" "$target" "$watched/work" "$watched" "$state"
  if [[ "$FAIL_COUNT" -eq "$fails_before" ]]; then
    rm -rf "$sandbox"
  fi
}
run_absolute "absolute-existing-directory" "existing"
run_absolute "absolute-new-directory" "new"

# A single relative segment of letters and hyphens must still scaffold under cwd.
safe_name() {
  local case_name="safe-single-segment"
  local project_name="safe-artifact"
  local sandbox
  sandbox="$(mktemp -d "${TMPDIR:-/tmp}/init-artifact-test.XXXXXX")"
  local watched="$sandbox/tree"
  local state="$sandbox/state"
  local work="$watched/work"
  mkdir -p "$work" "$state"
  local log="$state/init.log"
  local pid
  pushd "$work" >/dev/null
  env -u CDPATH bash "$INIT_SCRIPT" "$project_name" </dev/null >"$log" 2>&1 &
  pid=$!
  popd >/dev/null
  KEEP_PIDS+=("$pid")
  local marker="$work/$project_name/package.json"
  local i
  for i in $(seq 1 240); do
    if [[ -f "$marker" ]]; then
      break
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.25
  done
  stop_init "$pid"

  if [[ ! -f "$marker" ]]; then
    fail "$case_name" "script did not create $project_name under the working directory"
    echo "    log: $log"
    return
  fi
  local outside
  outside="$(find "$watched" -mindepth 1 ! -path "$work" ! -path "$work/*" -printf '%P\n' | head -n 5 || true)"
  if [[ -n "$outside" ]]; then
    fail "$case_name" "safe name wrote outside the working directory: $outside"
    echo "    log: $log"
    return
  fi
  pass "$case_name"
  rm -rf "$sandbox"
}

safe_name

echo
echo "$PASS_COUNT passed, $FAIL_COUNT failed"
if [[ "$FAIL_COUNT" -ne 0 ]]; then
  exit 1
fi
exit 0
