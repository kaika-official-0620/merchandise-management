#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${RENDER_TESSERACT_ROOT:-$PWD/.render/tesseract}"
CACHE_DIR="$PWD/.render/apt-cache"

has_required_tesseract() {
  if ! command -v tesseract >/dev/null 2>&1; then
    return 1
  fi
  local languages
  languages="$(tesseract --list-langs 2>/dev/null || true)"
  printf '%s\n' "$languages" | grep -qx "eng" &&
    printf '%s\n' "$languages" | grep -qx "jpn"
}

mkdir -p "$INSTALL_ROOT" "$CACHE_DIR"

if has_required_tesseract; then
  echo "tesseract already available: $(command -v tesseract)"
  tesseract --version
  tesseract --list-langs
  exit 0
fi

if command -v apt-get >/dev/null 2>&1; then
  if apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-jpn; then
    if has_required_tesseract; then
      echo "installed tesseract via apt-get"
      tesseract --version
      tesseract --list-langs
      exit 0
    fi
  fi
fi

if ! command -v apt-cache >/dev/null 2>&1 || ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "apt-cache and dpkg-deb are required for user-space tesseract install" >&2
  exit 1
fi

apt-get update || true

PACKAGE_LIST="$(
  (apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
    --no-breaks --no-replaces --no-enhances \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-jpn 2>/dev/null || true) |
  awk '
    /^[[:alnum:]][[:alnum:].+:-]+$/ { print $1 }
    /^[[:space:]]*(Pre)?Depends:/ {
      name=$2
      gsub(/[<>]/, "", name)
      if (name != "") print name
    }
  ' |
  sort -u
)"

PACKAGE_LIST="$(printf '%s\n%s\n' "tesseract-ocr tesseract-ocr-eng tesseract-ocr-jpn" "$PACKAGE_LIST" | tr ' ' '\n' | sort -u)"

cd "$CACHE_DIR"
for package_name in $PACKAGE_LIST; do
  apt-get download "$package_name" || true
done

shopt -s nullglob
deb_files=(./*.deb)
if [ "${#deb_files[@]}" -eq 0 ]; then
  echo "no deb files downloaded for tesseract" >&2
  exit 1
fi

for deb in "${deb_files[@]}"; do
  dpkg-deb -x "$deb" "$INSTALL_ROOT"
done

export PATH="$INSTALL_ROOT/usr/bin:$PATH"
export LD_LIBRARY_PATH="$INSTALL_ROOT/usr/lib/x86_64-linux-gnu:$INSTALL_ROOT/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"

for tessdata_dir in \
  "$INSTALL_ROOT/usr/share/tesseract-ocr/5/tessdata" \
  "$INSTALL_ROOT/usr/share/tesseract-ocr/4.00/tessdata" \
  "$INSTALL_ROOT/usr/share/tessdata"; do
  if [ -d "$tessdata_dir" ]; then
    export TESSDATA_PREFIX="$tessdata_dir"
    break
  fi
done

if ! has_required_tesseract; then
  echo "tesseract binary or required languages (eng, jpn) were not installed" >&2
  exit 1
fi

tesseract --version
tesseract --list-langs
