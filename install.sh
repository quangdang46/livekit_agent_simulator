#!/usr/bin/env bash
# Install lksr (the Rust build of livekit-agent-simulator) from GitHub Releases.
# Single static binary, SHA256-verified — no uv/pip/build on the user machine.
#
#   curl -fsSL "https://github.com/quangdang46/livekit-agent-simulator/releases/download/v0.1.0-rust/install.sh" | bash
#
set -euo pipefail
umask 022

BINARY_NAME="lksr"
MCP_SERVER_NAME="livekit-agent-simulator"
PKG_NAME="livekit-agent-simulator"
OWNER="quangdang46"
REPO="livekit-agent-simulator"
DEST="${DEST:-$HOME/.local/bin}"
# Optional override root. When set, the binary lands under $INSTALL_ROOT/current/
# with a symlink at $DEST/lksr; when unset (default) it installs directly to $DEST.
INSTALL_ROOT="${INSTALL_ROOT:-}"
CURRENT_DIR="${INSTALL_ROOT:+$INSTALL_ROOT/current}"
GIT_REF="${LK_SIM_REF:-}"
QUIET=0
EASY=0
VERIFY=0
UNINSTALL=0
NO_MCP=0
LOCK_DIR="${TMPDIR:-/tmp}/${BINARY_NAME}-install.lock.d"

log_info()    { [ "$QUIET" -eq 1 ] && return; echo "[${BINARY_NAME}] $*" >&2; }
log_warn()    { echo "[${BINARY_NAME}] WARN: $*" >&2; }
log_success() { [ "$QUIET" -eq 1 ] && return; echo "OK $*" >&2; }
die()         { echo "ERROR: $*" >&2; exit 1; }

cleanup() { rm -rf "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ >"$LOCK_DIR/pid"
    return 0
  fi
  die "Another install is running. If stuck: rm -rf $LOCK_DIR"
}

usage() {
  cat <<EOF
Install ${BINARY_NAME} (Rust build of ${PKG_NAME}) from GitHub Releases.
Single static binary, SHA256-verified. No uv/pip on the user machine.

  curl -fsSL "https://github.com/${OWNER}/${REPO}/releases/download/v0.1.0-rust/install.sh" | bash
  curl -fsSL "https://github.com/${OWNER}/${REPO}/releases/download/v0.1.0-rust/install.sh" | bash -s -- --ref v0.1.0-rust --verify

Default ref = latest release.

Options:
  --version / --ref REF   release tag (default: latest)
  --no-mcp                skip MCP provider auto-config
  --easy-mode             append DEST to PATH in shell rc
  --verify                run ${BINARY_NAME} --version + guide
  --quiet, -q
  --uninstall
  -h, --help
EOF
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --version|--ref)   GIT_REF="$2"; shift 2 ;;
    --version=*|--ref=*) GIT_REF="${1#*=}"; shift ;;
    --from-git|--from-git=*) shift; log_warn "--from-git ignored (release-binary installer)" ;;
    --no-mcp)          NO_MCP=1; shift ;;
    --easy-mode)       EASY=1; shift ;;
    --verify)          VERIFY=1; shift ;;
    --quiet|-q)        QUIET=1; shift ;;
    --uninstall)       UNINSTALL=1; shift ;;
    -h|--help)         usage ;;
    *)                 shift ;;
  esac
done

maybe_add_path() {
  case ":$PATH:" in *":$DEST:"*) return 0 ;; esac
  if [ "$EASY" -eq 1 ]; then
    for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.zprofile"; do
      [ -f "$rc" ] && [ -w "$rc" ] || continue
      grep -qF "$DEST" "$rc" 2>/dev/null && continue
      printf '\nexport PATH="%s:$PATH"  # %s installer\n' "$DEST" "$BINARY_NAME" >>"$rc"
      log_info "PATH += $DEST in $rc"
    done
  fi
  log_warn "Ensure CLI on PATH: export PATH=\"$DEST:\$PATH\""
}

_json_merge() {
  local file="$1" key="$2" value="$3"
  mkdir -p "$(dirname "$file")"
  if [ ! -f "$file" ]; then
    printf '{\n  "%s": %s\n}\n' "$key" "$value" >"$file"
    return 0
  fi
  if command -v jq >/dev/null 2>&1; then
    local tmpf; tmpf="$(mktemp)"
    jq --argjson val "$value" --arg k "$key" '
      if has($k) then .[$k] = ((.[$k] // {}) + $val) else .[$k] = $val end
    ' "$file" >"$tmpf" && mv "$tmpf" "$file"
  elif command -v python3 >/dev/null 2>&1; then
    KEY="$key" VAL="$value" FILE="$file" python3 - <<'PY'
import json, os
f, k = os.environ["FILE"], os.environ["KEY"]
v = json.loads(os.environ["VAL"])
try:
    d = json.load(open(f, encoding="utf-8")) if os.path.getsize(f) else {}
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
cur = d.get(k) if isinstance(d.get(k), dict) else {}
cur.update(v)
d[k] = cur
with open(f, "w", encoding="utf-8") as out:
    json.dump(d, out, indent=2)
    out.write("\n")
PY
  else
    log_warn "No jq/python3 - skip JSON merge for $file"
    return 1
  fi
}

_remove_mcp_from_file() {
  local file="$1" server="$2" parent="${3:-mcpServers}"
  [ -f "$file" ] || return 0
  if command -v jq >/dev/null 2>&1; then
    local tmpf; tmpf="$(mktemp)"
    jq --arg p "$parent" --arg s "$server" 'if .[$p] then del(.[$p][$s]) else . end' "$file" >"$tmpf" && mv "$tmpf" "$file"
  elif command -v python3 >/dev/null 2>&1; then
    FILE="$file" PARENT="$parent" SERVER="$server" python3 - <<'PY'
import json, os
f, p, s = os.environ["FILE"], os.environ["PARENT"], os.environ["SERVER"]
try:
    d = json.load(open(f, encoding="utf-8"))
except Exception:
    raise SystemExit(0)
if isinstance(d, dict) and isinstance(d.get(p), dict):
    d[p].pop(s, None)
    with open(f, "w", encoding="utf-8") as out:
        json.dump(d, out, indent=2)
        out.write("\n")
PY
  fi
}

resolve_lk_sim() {
  # Primary `lksr`.
  if command -v "$BINARY_NAME" >/dev/null 2>&1; then
    command -v "$BINARY_NAME"
    return 0
  fi
  for c in "$DEST/$BINARY_NAME" "$CURRENT_DIR/$BINARY_NAME"; do
    [ -x "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

configure_all_mcp_providers() {
  local lk_bin
  lk_bin=$(resolve_lk_sim) || {
    log_warn "lksr not found - skip MCP config"
    return 0
  }
  log_info "MCP providers -> ${lk_bin} mcp"
  local mcp_entry
  mcp_entry=$(cat <<EOF
{
  "${MCP_SERVER_NAME}": {
    "command": "${lk_bin}",
    "args": ["mcp"],
    "env": {}
  }
}
EOF
)
  _json_merge "$HOME/.claude.json" "mcpServers" "$mcp_entry" || true
  _json_merge "$HOME/.cursor/mcp.json" "mcpServers" "$mcp_entry" || true
  _json_merge "$HOME/.codeium/windsurf/mcp_config.json" "mcpServers" "$mcp_entry" || true
  _json_merge "$HOME/.vscode/mcp.json" "servers" "$mcp_entry" || true
  _json_merge "$HOME/.gemini/settings.json" "mcpServers" "$mcp_entry" || true
}

uninstall_all() {
  log_info "Uninstalling ${PKG_NAME}..."
  rm -f "$DEST/lksr" "$DEST/lks" "$DEST/lks-mcp" 2>/dev/null || true
  [ -n "$INSTALL_ROOT" ] && rm -rf "$INSTALL_ROOT" 2>/dev/null || true
  _remove_mcp_from_file "$HOME/.claude.json" "$MCP_SERVER_NAME"
  _remove_mcp_from_file "$HOME/.cursor/mcp.json" "$MCP_SERVER_NAME"
  _remove_mcp_from_file "$HOME/.vscode/mcp.json" "$MCP_SERVER_NAME" "servers"
  log_success "Uninstalled ${PKG_NAME}"
  exit 0
}

latest_release_tag() {
  curl -fsSL "https://api.github.com/repos/${OWNER}/${REPO}/releases/latest" 2>/dev/null \
    | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
}

resolve_install_ref() {
  if [ -n "${GIT_REF}" ]; then
    echo "${GIT_REF}"
    return 0
  fi
  local latest
  latest="$(latest_release_tag || true)"
  [ -n "$latest" ] || die "No GitRef and no GitHub releases. Pass --ref v0.1.0-rust"
  log_info "Default ref -> latest release ${latest}"
  echo "$latest"
}

release_tag_from_ref() {
  local ref="$1"
  case "$ref" in
    v[0-9]*.[0-9]*) echo "$ref" ;;
    [0-9]*.[0-9]*)  echo "v${ref}" ;;
    *)              echo "" ;;
  esac
}

asset_name() {
  local os arch
  case "$(uname -s)" in
    Darwin) os="macos" ;;
    Linux)  os="linux" ;;
    *) die "unsupported OS: $(uname -s). On Windows use install.ps1 (Python build)." ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) arch="x86_64" ;;
    arm64|aarch64) arch="aarch64" ;;
    *) die "unsupported arch: $(uname -m)" ;;
  esac
  echo "lksr-${os}-${arch}.tar.gz"
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    die "no sha256sum/shasum available for checksum verification"
  fi
}

install_binary() {
  local ref="$1" tag asset url work tarball bin expected actual
  tag="$(release_tag_from_ref "$ref")"
  [ -n "$tag" ] || die "Release binary install requires a version tag (e.g. v0.1.0-rust), not '$ref'"

  asset="$(asset_name)"
  log_info "Looking for ${BINARY_NAME} release ${tag}: ${asset}"

  url="$(curl -fsSL "https://api.github.com/repos/${OWNER}/${REPO}/releases/tags/${tag}" \
    | sed -n "s/.*\"browser_download_url\":[[:space:]]*\"\\([^\"]*${asset}\\)\".*/\\1/p" \
    | head -1)"
  [ -n "$url" ] || die "Release ${tag} missing asset ${asset}"

  work="$(mktemp -d "${TMPDIR:-/tmp}/lksr-install.XXXXXX")"
  tarball="${work}/${asset}"
  log_info "Downloading ${url}"
  curl -fsSL "$url" -o "$tarball"
  [ -s "$tarball" ] || die "empty download"

  log_info "Verifying SHA256..."
  curl -fsSL "https://github.com/${OWNER}/${REPO}/releases/download/${tag}/SHA256SUMS.txt" -o "$work/SHA256SUMS.txt" \
    || die "release ${tag} has no SHA256SUMS.txt"
  expected="$(awk -v a="$asset" '$2==a {print $1; exit}' "$work/SHA256SUMS.txt")"
  [ -n "$expected" ] || die "SHA256SUMS.txt has no entry for ${asset}"
  actual="$(sha256_of "$tarball")"
  [ "$expected" = "$actual" ] || die "SHA256 mismatch for ${asset} (expected ${expected}, got ${actual})"
  log_info "SHA256 OK (${expected})"

  log_info "Extracting..."
  tar -xzf "$tarball" -C "$work"
  bin="$(find "$work" -mindepth 1 -maxdepth 2 -type f -name "${BINARY_NAME}" | head -1)"
  [ -n "$bin" ] || die "lksr binary not found in tarball"

  if [ -n "$CURRENT_DIR" ]; then
    mkdir -p "$CURRENT_DIR" "$DEST"
    install -m 0755 "$bin" "$CURRENT_DIR/lksr"
    ln -sfn "$CURRENT_DIR/lksr" "$DEST/lksr"
    log_success "Installed -> $CURRENT_DIR/lksr (symlink in $DEST/lksr)"
  else
    mkdir -p "$DEST"
    install -m 0755 "$bin" "$DEST/lksr"
    log_success "Installed -> $DEST/lksr"
  fi

  rm -rf "$work"
}

[ "$UNINSTALL" -eq 1 ] && uninstall_all

main() {
  acquire_lock
  mkdir -p "$DEST"

  local ref
  ref="$(resolve_install_ref)"

  log_info "Installing ${PKG_NAME} Rust binary (ref ${ref})"
  log_info "Single static binary - no uv/pip/build on this machine"
  install_binary "$ref"

  export PATH="${DEST}:${PATH}"
  maybe_add_path

  if [ "$NO_MCP" -eq 0 ]; then
    configure_all_mcp_providers
  else
    log_info "Skipped MCP auto-config (--no-mcp)"
  fi

  if [ "$VERIFY" -eq 1 ]; then
    local lk
    lk="$(resolve_lk_sim 2>/dev/null || true)"
    [ -n "$lk" ] || die "${BINARY_NAME} not on PATH after install"
    "$lk" --version >/dev/null && "$lk" guide >/dev/null
    log_success "Verified ${BINARY_NAME} --version + guide"
  fi

  echo ""
  log_success "${PKG_NAME} installed"
  if lkb=$(resolve_lk_sim 2>/dev/null); then
    echo "  CLI:  $lkb"
    echo "  MCP:  $lkb mcp"
  fi
  echo "  Bin:  $DEST/lksr"
  echo ""
  echo "  Quick start:"
  echo "    ${BINARY_NAME} guide"
  echo "    ${BINARY_NAME} init --root /path/to/target"
  echo "    ${BINARY_NAME} web --root /path/to/target"
  echo "    ${BINARY_NAME} mcp"
  echo ""
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]] || [[ -z "${BASH_SOURCE[0]:-}" ]]; then
  { main "$@"; }
fi
