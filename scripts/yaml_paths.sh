#!/usr/bin/env bash
# yaml_paths.sh - switch device YAML paths between local (dev) and remote (release).
#
# ─── Convention ───
# Each YAML carries ONE active value per resource: ext_components_source,
# assets_base, and each `packages:` entry. No dual-mode commented shadow
# lines. This script is the single source of truth for those values:
# rewriting in place from local→remote (or vice versa) is mechanical, no
# manual edit needed.
#
# Workflow:
# - Dev: working tree in local mode. `esphome compile` picks up uncommitted
#   changes to `esphome/components/` and `packages/` directly.
# - Pre-release: `./yaml_paths.sh remote --branch <release>`, commit, push,
#   tag. End users compile straight from github://, no clone needed.
#
# ─── How it works ───
# Three rewrite targets per YAML:
#   1. ext_components_source  → string substitution (single line)
#   2. assets_base            → string substitution (single line, optional;
#                                some yamls don't use external assets)
#   3. packages: <key>: VALUE → one line per package entry (N entries)
#
# For (1)+(2): straight `sed` of the active value.
# For (3): per-line awk replacement, because the path conversion needs
# `realpath --relative-to` per yaml (each yaml lives at a different depth).
#
# ─── ESPHome conventions we rely on ───
# - `external_components: source: github://OWNER/REPO@BRANCH` defaults to
#   looking up components in the repo's `esphome/components/` subfolder.
#   So local equivalent must be `<reldepth>/esphome/components` (full path
#   to that subfolder), not just the repo root.
# - `packages: <key>: github://OWNER/REPO/<inner_path>@BRANCH` resolves
#   to the file at `<inner_path>` inside the repo at that branch. Local
#   equivalent: `!include <relpath_to_that_file>`.
# - `assets_base` is just a string substitution prefix used by the YAML's
#   own `image:`/`font:` blocks. Local: `<reldepth>/`. Remote: full HTTPS
#   raw URL `https://github.com/OWNER/REPO/raw/BRANCH/`.
#
# ─── Edge cases handled ───
# - Package keys with digits (`s3_base`, `status_led`): regex uses
#   `[a-zA-Z_][a-zA-Z0-9_]*` (identifier shape, not just letters).
# - Yaml depth varies (3 or 4 levels deep under `yamls/`): all paths
#   computed dynamically via `realpath --relative-to`. No hardcoded depth.
# - Yamls without `assets_base` (e.g. minimal intercom-only): the sed
#   substitution is a no-op when the line is absent.
# - Roundtrip local→remote→local is byte-identical (verified via md5sum).

set -euo pipefail

# ────────── Defaults ──────────
DEFAULT_URL="github://n-IA-hane/esphome-intercom"
ASSETS_HOST="https://github.com"   # for assets_base remote URL composition

# ────────── Helpers ──────────
err()  { echo "error: $*" >&2; exit 1; }
log()  { echo "$*" >&2; }
note() { echo "  $*" >&2; }

repo_root() {
  git rev-parse --show-toplevel 2>/dev/null || err "not in a git repo"
}

current_branch() {
  git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main"
}

# Strip "github://OWNER/REPO" → "OWNER/REPO"
url_owner_repo() {
  echo "$1" | sed -E 's|^github://([^/]+/[^/@]+).*|\1|'
}

# Find production YAMLs that should be toggle-managed.
# Excludes: ESPHome build cache, secrets file (per-device WiFi credentials,
# never published), `yamls/debug` / `yamls/host` (local diagnostics, not
# downloadable release presets), and `*_NOT_READY.yaml` (work-in-progress
# yamls staged but not yet wired up to the toggle pattern).
find_yamls() {
  local root="$1"
  find "$root/yamls" -type f -name '*.yaml' \
    -not -path '*/.esphome/*' \
    -not -path "$root/yamls/debug/*" \
    -not -path "$root/yamls/host/*" \
    -not -name 'secrets.yaml' \
    -not -name '*_NOT_READY.yaml' \
    | sort
}

# Classify a YAML: "local" if all toggle sites use relative paths/!include,
# "remote" if all use github://, "mixed" if both forms appear (= manual edit
# left it inconsistent, lint should fail), "unknown" if no toggle site found
# (yaml doesn't actually use the toggle pattern, e.g. a fragment).
detect_mode() {
  local f="$1" has_local=0 has_remote=0
  # Remote markers: ext_components_source pointing at github://, OR any
  # packages: entry with github:// shorthand.
  if grep -qE '^[[:space:]]*ext_components_source:[[:space:]]*"github://' "$f" \
     || grep -qE '^[[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*github://' "$f"; then
    has_remote=1
  fi
  # Local markers: ext_components_source with relative path (`"../`), OR any
  # packages: entry using !include yaml tag.
  if grep -qE '^[[:space:]]*ext_components_source:[[:space:]]*"\.\./' "$f" \
     || grep -qE '^[[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*!include' "$f"; then
    has_local=1
  fi
  if [[ $has_local -eq 1 && $has_remote -eq 1 ]]; then echo "mixed"
  elif [[ $has_remote -eq 1 ]]; then echo "remote"
  elif [[ $has_local -eq 1 ]]; then echo "local"
  else echo "unknown"; fi
}

# Rewrite a single YAML to LOCAL mode (relative paths).
# `relroot` is the relative path from the yaml's directory to the repo root,
# computed dynamically so depth differences across yamls (3 vs 4 levels) are
# handled automatically. With trailing slash unless yaml IS at repo root
# (degenerate case, none of our yamls live there).
to_local() {
  local f="$1" root="$2" yaml_dir relroot
  yaml_dir=$(dirname "$f")
  relroot=$(realpath --relative-to="$yaml_dir" "$root")
  [[ "$relroot" == "." ]] && relroot="" || relroot="$relroot/"

  # 1) ext_components_source → "<relroot>esphome/components"
  #    e.g. yaml at depth 3 ⇒ relroot="../../../" ⇒ value "../../../esphome/components"
  sed -i -E "s|^([[:space:]]*ext_components_source:)[[:space:]]*\"[^\"]*\"|\1 \"${relroot}esphome/components\"|" "$f"

  # 2) assets_base → "<relroot>"
  #    Bare relroot (e.g. "../../../") points to the repo root, where assets/
  #    and similar siblings live. If yaml IS at repo root, fall back to "./".
  local assets_value="${relroot}"
  [[ -z "$assets_value" ]] && assets_value="./"
  sed -i -E "s|^([[:space:]]*assets_base:)[[:space:]]*\"[^\"]*\"|\1 \"${assets_value}\"|" "$f"

  # 3) packages: each "<key>: github://OWNER/REPO/INNER@BRANCH" → "<key>: !include <relpath>"
  # Per-line because the relative path back to each package file depends on
  # the yaml's location. Steps:
  #   a. grep all package lines with their line numbers
  #   b. for each line, parse out indent, key, INNER (path inside repo),
  #      branch (discarded, going local).
  #   c. compute target_abs (where INNER lives in the working tree) and
  #      target_rel (path from this yaml's directory to that target).
  #   d. replace the line with "<indent><key>: !include <target_rel>".
  # awk used instead of sed for the final replacement: the new line contains
  # path separators that would need escaping in sed substitutions.
  local lines
  lines=$(grep -nE '^[[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*github://' "$f" || true)
  if [[ -n "$lines" ]]; then
    while IFS= read -r line_info; do
      local lineno orig key inner target_abs target_rel indent new
      lineno=$(echo "$line_info" | cut -d: -f1)
      orig=$(sed -n "${lineno}p" "$f")
      key=$(echo "$orig" | sed -E 's|^[[:space:]]*([a-zA-Z_][a-zA-Z0-9_]*):.*|\1|')
      indent=$(echo "$orig" | sed -E 's|^( *).*|\1|')
      # INNER = path between repo + the @branch suffix:
      #   "github://owner/repo/packages/foo.yaml@branch" → "packages/foo.yaml"
      inner=$(echo "$orig" | sed -E 's|^[[:space:]]*[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*github://[^/]+/[^/]+/(.+)@[^[:space:]]*[[:space:]]*$|\1|')
      target_abs="$root/$inner"
      target_rel=$(realpath --relative-to="$yaml_dir" "$target_abs")
      new="${indent}${key}: !include ${target_rel}"
      awk -v ln="$lineno" -v repl="$new" 'NR==ln{print repl; next}{print}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    done <<< "$lines"
  fi
}

# Rewrite a single YAML to REMOTE mode (github://owner/repo paths + branch).
# Symmetric to to_local but inverse direction. Reads the local relpath of
# each package, resolves it to an absolute path, then computes the path
# relative to the repo root (= the INNER part of the github:// URL).
to_remote() {
  local f="$1" root="$2" url="$3" branch="$4"
  local yaml_dir owner_repo assets_url
  yaml_dir=$(dirname "$f")
  owner_repo=$(url_owner_repo "$url")
  # assets_base remote uses HTTPS raw URLs (not the github:// shorthand)
  # because it's just a string concatenated with image/font paths in YAML,
  # not consumed by ESPHome's github:// resolver.
  assets_url="${ASSETS_HOST}/${owner_repo}/raw/${branch}/"

  # 1) ext_components_source → "github://OWNER/REPO@BRANCH"
  #    No subfolder needed: ESPHome defaults to <repo>/esphome/components/.
  sed -i -E "s|^([[:space:]]*ext_components_source:)[[:space:]]*\"[^\"]*\"|\1 \"${url}@${branch}\"|" "$f"

  # 2) assets_base → "https://github.com/OWNER/REPO/raw/BRANCH/"
  sed -i -E "s|^([[:space:]]*assets_base:)[[:space:]]*\"[^\"]*\"|\1 \"${assets_url}\"|" "$f"

  # 3) packages.
  # If a YAML is already remote, retarget existing github:// package entries
  # that point to this repo first. This is the release case: dev -> main
  # should not require a local-mode roundtrip just to change the branch suffix.
  #
  # Keep third-party github:// packages untouched. Some upstream release YAMLs
  # deliberately live on a non-main branch, and rewriting those would create a
  # broken preset even though our own packages/components are correctly remote.
  local remote_lines
  remote_lines=$(grep -nE '^[[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*github://' "$f" || true)
  if [[ -n "$remote_lines" ]]; then
    while IFS= read -r line_info; do
      local lineno orig prefix value inner new
      lineno=$(echo "$line_info" | cut -d: -f1)
      orig=$(sed -n "${lineno}p" "$f")
      prefix=$(echo "$orig" | sed -E 's|^([[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*).*|\1|')
      value=$(echo "$orig" | sed -E 's|^[[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*([^[:space:]]+).*$|\1|')
      if [[ "$value" == "$url/"*@* ]]; then
        inner="${value#"$url/"}"
        inner="${inner%@*}"
        new="${prefix}${url}/${inner}@${branch}"
        awk -v ln="$lineno" -v repl="$new" 'NR==ln{print repl; next}{print}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
      fi
    done <<< "$remote_lines"
  fi

  # Then convert local includes:
  # each "<key>: !include <relpath>" -> "<key>: github://OWNER/REPO/INNER@BRANCH"
  # Steps mirror to_local in reverse:
  #   a. parse indent, key, relpath from the !include line.
  #   b. realpath -m to absolute (with -m so it works even if a future
  #      reorganisation has moved a file: doesn't error out on missing).
  #   c. realpath --relative-to repo_root → INNER (path inside the repo).
  #   d. write "<indent><key>: <url>/<inner>@<branch>".
  local lines
  lines=$(grep -nE '^[[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*!include[[:space:]]' "$f" || true)
  if [[ -n "$lines" ]]; then
    while IFS= read -r line_info; do
      local lineno orig key relpath target_abs inner indent new
      lineno=$(echo "$line_info" | cut -d: -f1)
      orig=$(sed -n "${lineno}p" "$f")
      key=$(echo "$orig" | sed -E 's|^[[:space:]]*([a-zA-Z_][a-zA-Z0-9_]*):.*|\1|')
      indent=$(echo "$orig" | sed -E 's|^( *).*|\1|')
      relpath=$(echo "$orig" | sed -E 's|^[[:space:]]*[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*!include[[:space:]]+(.+)$|\1|')
      target_abs=$(realpath -m "$yaml_dir/$relpath")
      inner=$(realpath --relative-to="$root" "$target_abs")
      new="${indent}${key}: ${url}/${inner}@${branch}"
      awk -v ln="$lineno" -v repl="$new" 'NR==ln{print repl; next}{print}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    done <<< "$lines"
  fi
}

# ────────── Commands ──────────
cmd_status() {
  local root branch
  root=$(repo_root)
  branch=$(current_branch)
  log "Repo:           $root"
  log "Current branch: $branch"
  log ""
  printf "%-65s  %s\n" "YAML" "MODE"
  printf "%-65s  %s\n" "-----------------------------------------------------------------" "-------"
  while IFS= read -r f; do
    local rel mode
    rel=$(realpath --relative-to="$root" "$f")
    [[ -n "${ONLY_FILE:-}" && "$rel" != "$ONLY_FILE" && "$f" != "$ONLY_FILE" ]] && continue
    mode=$(detect_mode "$f")
    printf "%-65s  %s\n" "$rel" "$mode"
  done < <(find_yamls "$root")
}

cmd_local() {
  local root
  root=$(repo_root)
  log "Switching to LOCAL mode (relative paths)"
  log ""
  while IFS= read -r f; do
    local rel
    rel=$(realpath --relative-to="$root" "$f")
    [[ -n "${ONLY_FILE:-}" && "$rel" != "$ONLY_FILE" && "$f" != "$ONLY_FILE" ]] && continue
    to_local "$f" "$root"
    note "$rel"
  done < <(find_yamls "$root")
}

cmd_remote() {
  local root url branch
  root=$(repo_root)
  url="${URL_ARG:-$DEFAULT_URL}"
  branch="${BRANCH_ARG:-main}"
  log "Switching to REMOTE mode"
  log "  URL:    $url"
  log "  Branch: $branch"
  log ""
  while IFS= read -r f; do
    local rel
    rel=$(realpath --relative-to="$root" "$f")
    [[ -n "${ONLY_FILE:-}" && "$rel" != "$ONLY_FILE" && "$f" != "$ONLY_FILE" ]] && continue
    to_remote "$f" "$root" "$url" "$branch"
    note "$rel"
  done < <(find_yamls "$root")
}

cmd_check() {
  local root rc=0
  root=$(repo_root)
  while IFS= read -r f; do
    local rel mode
    rel=$(realpath --relative-to="$root" "$f")
    mode=$(detect_mode "$f")
    if [[ "$mode" == "mixed" || "$mode" == "unknown" ]]; then
      log "FAIL: $rel ($mode)"
      rc=1
    fi
    if grep -qE '^[[:space:]]*-[[:space:]]*!include[[:space:]]+' "$f"; then
      log "FAIL: $rel (nested list !include is not portable outside the repo)"
      rc=1
    fi
  done < <(find_yamls "$root")
  if [[ $rc -eq 0 ]]; then log "OK: all YAMLs consistent."; fi
  exit $rc
}

usage() {
  cat <<EOF
yaml_paths.sh - switch device YAML paths between local (dev) and remote (release).

Usage:
  $(basename "$0") <command> [options]

Commands:
  status                          Print mode (local/remote/mixed) per YAML
  local                           Rewrite all YAMLs to LOCAL mode (dev)
  remote [--url U] [--branch B]   Rewrite all YAMLs to REMOTE mode (release)
  check                           Lint: fail on mixed paths or nested list !include

Options:
  --url URL       e.g. github://n-IA-hane/esphome-intercom (default)
  --branch B      branch shorthand (default: main)
  --file PATH     limit operation to a single YAML (relative to repo root or absolute)

Examples:
  $(basename "$0") status
  $(basename "$0") local
  $(basename "$0") remote --branch dev
  $(basename "$0") remote --url github://my-fork/esphome-intercom --branch dev
  $(basename "$0") remote --file yamls/intercom-only/single-bus/xiaozhi-intercom.yaml --branch main
EOF
}

# ────────── Arg parsing ──────────
[[ $# -lt 1 ]] && { usage; exit 1; }

cmd="$1"; shift
URL_ARG=""
BRANCH_ARG=""
ONLY_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)    URL_ARG="$2"; shift 2 ;;
    --branch) BRANCH_ARG="$2"; shift 2 ;;
    --file)   ONLY_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown option: $1 (run --help)" ;;
  esac
done

case "$cmd" in
  status) cmd_status ;;
  local)  cmd_local ;;
  remote) cmd_remote ;;
  check)  cmd_check ;;
  -h|--help) usage ;;
  *) err "unknown command: $cmd (run --help)" ;;
esac
