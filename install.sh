#!/bin/sh
set -eu
version=${QUARRY_VERSION:-}
dir=${QUARRY_INSTALL_DIR:-"$HOME/.local/bin"}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --version) version=$2; shift 2;;
    --dir) dir=$2; shift 2;;
    *) echo 'Usage: install.sh [--version X.Y.Z[-rc.N]] [--dir PATH]' >&2; exit 2;;
  esac
done
case "$(uname -s)" in Darwin) os=macos;; Linux) os=linux;; *) echo 'Supported: macOS and Linux' >&2; exit 1;; esac
case "$(uname -m)" in arm64|aarch64) arch=arm64;; x86_64|amd64) arch=x86_64;; *) echo 'Unsupported architecture' >&2; exit 1;; esac
if [ -z "$version" ]; then
  version=$(curl -fsSL https://api.github.com/repos/yiminspace/quarry/releases/latest | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p' | head -n 1)
  case "$version" in 2.*) ;; *) echo 'Rust stable release is not published; select --version 2.0.0-rc.1 explicitly.' >&2; exit 1;; esac
fi
printf '%s\n' "$version" | grep -Eq '^2\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$' || { echo 'Invalid version' >&2; exit 1; }
asset="quarry-$version-$os-$arch.tar.gz"
base="https://github.com/yiminspace/quarry/releases/download/v$version"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
curl -fsSL "$base/$asset" -o "$tmp/$asset"
curl -fsSL "$base/SHA256SUMS" -o "$tmp/SHA256SUMS"
expected=$(awk -v f="$asset" '$2==f {print $1}' "$tmp/SHA256SUMS")
printf '%s\n' "$expected" | grep -Eq '^[a-f0-9]{64}$' || { echo 'Missing/invalid checksum' >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then actual=$(sha256sum "$tmp/$asset" | awk '{print $1}'); else actual=$(shasum -a 256 "$tmp/$asset" | awk '{print $1}'); fi
[ "$actual" = "$expected" ] || { echo 'Checksum mismatch' >&2; exit 1; }
tar -xzf "$tmp/$asset" -C "$tmp" qy
[ -f "$tmp/qy" ] && [ ! -L "$tmp/qy" ] || exit 1
"$tmp/qy" --version
mkdir -p "$dir"
staged=$(mktemp "$dir/.qy.XXXXXX")
cp "$tmp/qy" "$staged"
chmod 755 "$staged"
mv -f "$staged" "$dir/qy"
printf 'Installed %s/qy\nAdd %s to PATH if needed. Restart older qy GUI/keeper processes after their work finishes.\n' "$dir" "$dir"
