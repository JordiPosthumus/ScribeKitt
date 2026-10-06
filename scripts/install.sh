#!/bin/bash
# Install a published ScribeKitt build without Xcode, Homebrew, or sudo.
set -euo pipefail

version="${1:-latest}"
if [[ "$(printf '%s' "$version" | tr '[:upper:]' '[:lower:]')" == "latest" ]]; then
  # Resolve the newest published release (pre-releases included) from the
  # releases feed, which has no API rate limit. /releases/latest does not
  # work here because every ScribeKitt release is marked pre-release.
  feed="$(curl --fail --silent --show-error --location --retry 3 --connect-timeout 20 \
    "https://github.com/JordiPosthumus/ScribeKitt/releases.atom")"
  tag="$(printf '%s\n' "$feed" | sed -n 's|.*releases/tag/\(v[0-9][0-9.]*\)".*|\1|p' | head -1)"
  if [[ ! "$tag" =~ ^v[0-9]+\.[0-9]+$ ]]; then
    echo "Could not determine the latest ScribeKitt release. Install a fixed version instead: bash install.sh 210.21" >&2
    exit 1
  fi
  version="${tag#v}"
  echo "Latest ScribeKitt release: $version"
fi
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+$ ]]; then
  echo "Usage: bash install.sh VERSION (for example, 210.15)" >&2
  exit 1
fi
if [[ "$(uname -s)" != Darwin ]] || [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" != 1 ]]; then
  echo "ScribeKitt requires an Apple Silicon Mac (M1 or newer)." >&2
  exit 1
fi
os_major="$(sw_vers -productVersion | cut -d. -f1)"
if (( os_major < 14 )); then
  echo "ScribeKitt requires macOS 14 or newer." >&2
  exit 1
fi
if pgrep -x AudioWhisper >/dev/null; then
  echo "Quit ScribeKitt, SpeedyWhisper, or AudioWhisper from its menu bar, then run this command again." >&2
  exit 1
fi

destination="/Applications"
if [[ -d "$HOME/Applications/ScribeKitt.app" && ! -d /Applications/ScribeKitt.app ]]; then
  destination="$HOME/Applications"
elif [[ ! -w /Applications && ! -d /Applications/ScribeKitt.app ]]; then
  destination="$HOME/Applications"
fi
mkdir -p "$destination"
if [[ ! -w "$destination" ]]; then
  echo "You don’t have permission to replace $destination/ScribeKitt.app. Ask this Mac’s administrator to install it." >&2
  exit 1
fi

download="$(mktemp -d "${TMPDIR:-/tmp}/scribekitt-download.XXXXXX")"
staging=""
cleanup() {
  rm -rf "$download"
  if [[ -n "$staging" ]]; then rm -rf "$staging"; fi
}
trap cleanup EXIT
archive="ScribeKitt-$version.zip"
release="https://github.com/JordiPosthumus/ScribeKitt/releases/download/v$version"
echo "Downloading ScribeKitt ${version}..."
curl --fail --location --retry 3 --connect-timeout 20 "$release/$archive" -o "$download/$archive"
curl --fail --silent --show-error --location --retry 3 "$release/SHA256SUMS" -o "$download/SHA256SUMS"
expected="$(awk -v name="$archive" '$2 == name { print $1 }' "$download/SHA256SUMS")"
actual="$(shasum -a 256 "$download/$archive" | awk '{ print $1 }')"
if [[ ! "$expected" =~ ^[a-f0-9]{64}$ || "$actual" != "$expected" ]]; then
  echo "Download verification failed. Your existing app was not changed." >&2
  exit 1
fi
ditto -x -k "$download/$archive" "$download/extracted"
app="$download/extracted/ScribeKitt.app"
codesign --verify --deep --strict "$app"
if [[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist")" != com.audiowhisper.app ||
      "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$app/Contents/Info.plist")" != "$version" ]]; then
  echo "The downloaded app does not match the requested release." >&2
  exit 1
fi

staging="$(mktemp -d "$destination/.ScribeKitt-install.XXXXXX")"
ditto "$app" "$staging/ScribeKitt.app"
codesign --verify --deep --strict "$staging/ScribeKitt.app"
# Recheck after downloading so a recording started meanwhile is never interrupted.
if pgrep -x AudioWhisper >/dev/null; then
  echo "ScribeKitt is now running. Quit it before trying the installation again." >&2
  exit 1
fi
target="$destination/ScribeKitt.app"
backup=""
if [[ -e "$target" ]]; then
  backup="$HOME/Library/Application Support/AudioWhisper/backups/$(date +%Y-%m-%d_%H%M%S)-install-$version.noindex"
  mkdir -p "$(dirname "$backup")"
  mkdir "$backup"
  mv "$target" "$backup/ScribeKitt.app"
fi
if ! mv "$staging/ScribeKitt.app" "$target"; then
  if [[ -n "$backup" ]]; then mv "$backup/ScribeKitt.app" "$target"; fi
  echo "Installation failed; the previous app was restored." >&2
  exit 1
fi
echo "Installed ScribeKitt $version in $destination."
if [[ -n "$backup" ]]; then
  echo "Previous app saved in $backup"
  echo "Update complete. If ScribeKitt asks for microphone or recording-key permission, approve it once."
else
  echo "On first launch, choose Prepare ScribeKitt. Allow 6 GB of free space for setup."
fi
open "$target"
