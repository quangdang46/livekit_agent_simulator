#!/usr/bin/env bash
# Install lksr (the Rust build of livekit-agent-simulator) from GitHub Releases.
# Single static-ish binary — no uv/pip/build on the user machine.
#
#   curl -fsSL "https://github.com/quangdang46/livekit-agent-simulator/releases/download/v0.1.0-rust/install-rust.sh" | bash
#   curl -fsSL "…/install-rust.sh" | bash -s -- --verify
#
set -euo pipefail
umask 022

BINARY_NAME="lksr"
PKG_NAME="livekit-agent-simulator"
OWNER="quangdang46"
REPO="livekit-agent-simulator"
DEST="${DEST:-$HOME/.local/bin}"
GIT_REF="${LK_SIM_REF:-}"
QUIET=0
VERIFY=0
UNINSTALL=0

log_info()    { [ "$QUIET" -eq 1 ] && return; echo "[${BINARY_NAME}] $*" >&2; }
log_success() { [ "$QUIET" -eq 1 ] && return; echo "OK $*" >&2; }
die()         { echo "ERROR: $*" >&2; exit 1; }

usage() {
  cat <<EOF
Install ${BINARY_NAME} (Rust build of ${PKG_NAME}) from GitHub Releases.

  curl -fsSL "https://github.com/${OWNER}/${REPO}/releases/download/v0.1.0-rust/install-rust.sh" | bash

Options:
  --version / --ref REF   release tag (default: latest)
  --verify                run ${BINARY_NAME} --version
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
    --verify)          VERIFY=1; shift ;;
    --quiet|-q)        QUIET=1; shift ;;
    --uninstall)       UNINSTALL=1; shift ;;
    -h|--help)         usage ;;
    *)                 shift ;;
  esac
done

release_tag_from_ref() {
  local ref="$1"
  if [ -n "$ref" ]; then
    case "$ref" in v*) echo "$ref" ;; *) echo "v$ref" ;; esac
    return 0
  fi
  curl -fsSL "https://api.github.com/repos/${OWNER}/${REPO}/releases/latest" 2>/dev/null \
    | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
}

asset_name() {
  local os arch
  case "$(uname -s)" in
    Darwin) os="macos" ;;
    Linux)  os="linux" ;;
    *) die "unsupported OS: $(uname -s)" ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) arch="x86_64" ;;
    arm64|aarch64) arch="aarch64" ;;
    *) die "unsupported arch: $(uname -m)" ;;
  esac
  echo "lksr-${os}-${arch}.tar.gz"
}

install_binary() {
  local ref="$1" tag asset url work tarball
  tag="$(release_tag_from_ref "$ref")"
  [ -n "$tag" ] || die "No GitRef and no GitHub releases. Pass --ref v0.1.0"

  asset="$(asset_name)"
  log_info "Looking for ${BINARY_NAME} release ${tag}: ${asset}"

  url="$(curl -fsSL "https://api.github.com/repos/${OWNER}/${REPO}/releases/tags/${tag}" \
    | sed -n "s/.*\"browser_download_url\":[[:space:]]*\"\([^\"]*${asset}\)\".*/\1/p" \
    | head -1)"
  [ -n "$url" ] || die "Release ${tag} missing asset ${asset}"

  work="$(mktemp -d "${TMPDIR:-/tmp}/lksr-install.XXXXXX")"
  tarball="${work}/${asset}"
  log_info "Downloading ${url}"
  curl -fsSL "$url" -o "$tarball"
  [ -s "$tarball" ] || die "empty download"

  log_info "Extracting..."
  tar -xzf "$tarball" -C "$work"
  local bin
  bin="$(find "$work" -mindepth 1 -maxdepth 2 -type f -name "${BINARY_NAME}" | head -1)"
  [ -n "$bin" ] || die "lksr binary not found in tarball"

  mkdir -p "$DEST"
  install -m 0755 "$bin" "$DEST/${BINARY_NAME}"
  log_success "Installed -> $DEST/${BINARY_NAME}"

  case ":$PATH:" in *":$DEST:"*) ;; *)
    log_info "Ensure CLI on PATH: export PATH=\"$DEST:\$PATH\""
  ;; esac

  rm -rf "$work"
}

[ "$UNINSTALL" -eq 1 ] && { rm -f "$DEST/${BINARY_NAME}"; log_success "Uninstalled $DEST/${BINARY_NAME}"; exit 0; }

install_binary "$GIT_REF"

if [ "$VERIFY" -eq 1 ]; then
  "$DEST/${BINARY_NAME}" --version
  log_success "${BINARY_NAME} verify OK"
fi
