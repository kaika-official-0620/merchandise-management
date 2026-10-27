#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${RENDER_TESSERACT_ROOT:-$PWD/.render/tesseract}"
CACHE_DIR="$PWD/.render/apt-cache"
APT_LISTS_DIR="$PWD/.render/apt-state/lists"
APT_LOG_DIR="$PWD/.render/apt-log"

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

if ! command -v apt-get >/dev/null 2>&1 || ! command -v apt-cache >/dev/null 2>&1 || ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "apt-get, apt-cache and dpkg-deb are required for user-space tesseract install" >&2
  exit 1
fi

# Render's native build image can mount /var/lib/apt and /var/cache/apt read-only.
# All three APT operations share writable lists/caches while retaining the image's
# repository configuration, trusted keyrings and normal signature verification.
mkdir -p "$APT_LISTS_DIR/partial" "$CACHE_DIR/archives/partial" "$APT_LOG_DIR"
APT_OPTIONS=(
  -o "Dir::State::Lists=$APT_LISTS_DIR"
  -o "Dir::Cache=$CACHE_DIR"
  -o "Dir::Cache::archives=$CACHE_DIR/archives"
  -o "Dir::Cache::pkgcache=$CACHE_DIR/pkgcache.bin"
  -o "Dir::Cache::srcpkgcache=$CACHE_DIR/srcpkgcache.bin"
  -o "Dir::Log=$APT_LOG_DIR"
  -o "APT::Update::Error-Mode=any"
)

apt-get "${APT_OPTIONS[@]}" update

DEPENDENCY_TREE="$(apt-cache "${APT_OPTIONS[@]}" depends --recurse --no-recommends --no-suggests --no-conflicts \
  --no-breaks --no-replaces --no-enhances \
  tesseract-ocr tesseract-ocr-eng tesseract-ocr-jpn)"

PACKAGE_LIST="$(
  printf '%s\n' "$DEPENDENCY_TREE" |
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
  apt-get "${APT_OPTIONS[@]}" download "$package_name" || true
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
