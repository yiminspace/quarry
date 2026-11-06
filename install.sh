#!/bin/sh
set -eu
version=${QUARRY_VERSION:-}
dir=${QUARRY_INSTALL_DIR:-}
download_only=0
if [ "${1:-}" = "--download-only" ]; then
  download_only=1
  shift
fi
usage() {
  echo 'Usage: install.sh [--version X.Y.Z[-rc.N]] [--dir PATH]' >&2
  exit 2
}
github_curl() {
  curl -q -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 10 "$@"
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --version) [ "$#" -ge 2 ] || usage; version=$2; shift 2;;
    --dir) [ "$#" -ge 2 ] || usage; dir=$2; shift 2;;
    *) usage;;
  esac
done
[ -n "$dir" ] || dir="${HOME:?HOME is required without --dir}/.local/bin"
case "$dir" in /*) ;; *) dir="$(pwd -P)/$dir";; esac
case "$(uname -s)" in Darwin) os=macos;; Linux) os=linux;; *) echo 'Supported: macOS and Linux' >&2; exit 1;; esac
case "$(uname -m)" in arm64|aarch64) arch=arm64;; x86_64|amd64) arch=x86_64;; *) echo 'Unsupported architecture' >&2; exit 1;; esac
if [ -z "$version" ]; then
  version=$(github_curl --max-time 30 --max-filesize 1048576 https://api.github.com/repos/yiminspace/quarry/releases/latest | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p' | head -n 1)
  printf '%s\n' "$version" | grep -Eq '^2\.[0-9]+\.[0-9]+$' || { echo 'Rust stable release is not published; select an explicit published --version.' >&2; exit 1; }
fi
printf '%s\n' "$version" | grep -Eq '^2\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-rc\.(0|[1-9][0-9]*))?$' || { echo 'Invalid version' >&2; exit 1; }
asset="quarry-$version-$os-$arch.tar.gz"
base="https://github.com/yiminspace/quarry/releases/download/v$version"
tmp=$(mktemp -d)
staging=
cleanup() {
  rm -rf "$tmp"
  [ -z "$staging" ] || rm -rf "$staging"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
github_curl --max-time 120 --max-filesize 134217728 "$base/$asset" -o "$tmp/$asset"
github_curl --max-time 30 --max-filesize 1048576 "$base/SHA256SUMS" -o "$tmp/SHA256SUMS"
expected=$(awk -v f="$asset" '$2==f {print $1}' "$tmp/SHA256SUMS")
printf '%s\n' "$expected" | grep -Eq '^[a-f0-9]{64}$' || { echo 'Missing/invalid checksum' >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then actual=$(sha256sum "$tmp/$asset" | awk '{print $1}'); else actual=$(shasum -a 256 "$tmp/$asset" | awk '{print $1}'); fi
[ "$actual" = "$expected" ] || { echo 'Checksum mismatch' >&2; exit 1; }
tar -xzf "$tmp/$asset" -C "$tmp" qy
[ -f "$tmp/qy" ] && [ ! -L "$tmp/qy" ] || { echo 'Archive is missing the qy executable' >&2; exit 1; }
chmod 755 "$tmp/qy"
[ "$("$tmp/qy" --version)" = "qy $version" ] || { echo 'Release executable has an unexpected version' >&2; exit 1; }
mkdir -p "$dir"
dir=$(cd "$dir" && pwd -P)
if [ "$download_only" -eq 1 ]; then
  cp "$tmp/qy" "$dir/qy"
  exit 0
fi

launcher="$dir/qy"
store="$dir/.quarry"
link=.quarry/current
if [ -L "$launcher" ] && [ "$(readlink "$launcher")" != "$link" ]; then
  echo 'The selected directory has a qy launcher owned by another installer.' >&2
  exit 1
fi
if [ -e "$launcher" ] && [ ! -f "$launcher" ]; then
  echo 'The selected directory already contains a non-file qy entry.' >&2
  exit 1
fi
if [ -L "$launcher" ] && [ -x "$launcher" ] && [ "$("$launcher" --internal-quarry-install-protocol 2>/dev/null || :)" = 1 ]; then
  "$launcher" --internal-quarry-install "$dir" "$version" "$tmp/qy"
elif [ "$("$tmp/qy" --internal-quarry-install-protocol 2>/dev/null || :)" = 1 ]; then
  "$tmp/qy" --internal-quarry-install "$dir" "$version" "$tmp/qy"
else
  # Releases predating the installer protocol can still be installed explicitly.
  # Their qy has no background updater, so there is no update worker to lock out.
  release="$store/releases/$version"
  for directory in "$store" "$store/releases" "$release"; do
    [ ! -L "$directory" ] || { echo 'Invalid Quarry install directory' >&2; exit 1; }
    mkdir -p "$directory"
  done
  if [ -d "$store/current" ] || [ -d "$release/qy" ]; then
    echo 'Invalid Quarry install layout' >&2
    exit 1
  fi
  staging=$(mktemp -d "$store/.install.XXXXXX")
  cp "$tmp/qy" "$staging/qy"
  mv -f "$staging/qy" "$release/qy"
  if [ -f "$launcher" ] && [ ! -L "$launcher" ]; then
    backup=$(mktemp "$store/previous-qy-XXXXXX")
    cp -p "$launcher" "$backup"
  fi
  ln -s "releases/$version/qy" "$staging/current"
  mv -f "$staging/current" "$store/current"
  if [ ! -L "$launcher" ]; then
    ln -s "$link" "$staging/qy"
    mv -f "$staging/qy" "$launcher"
  fi
fi
printf 'Installed Quarry %s at %s/qy\nAdd %s to PATH if needed. Restart older qy GUI/keeper processes after their work finishes.\n' "$version" "$dir" "$dir"
