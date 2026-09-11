#!/usr/bin/env bash
set -euo pipefail

TESSERACT_ROOT="${RENDER_TESSERACT_ROOT:-$PWD/.render/tesseract}"

if [ -d "$TESSERACT_ROOT/usr/bin" ]; then
  export PATH="$TESSERACT_ROOT/usr/bin:$PATH"
fi

if [ -d "$TESSERACT_ROOT/usr/lib/x86_64-linux-gnu" ] || [ -d "$TESSERACT_ROOT/lib/x86_64-linux-gnu" ]; then
  export LD_LIBRARY_PATH="$TESSERACT_ROOT/usr/lib/x86_64-linux-gnu:$TESSERACT_ROOT/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
fi

if [ -z "${TESSDATA_PREFIX:-}" ]; then
  for tessdata_dir in \
    "$TESSERACT_ROOT/usr/share/tesseract-ocr/5/tessdata" \
    "$TESSERACT_ROOT/usr/share/tesseract-ocr/4.00/tessdata" \
    "$TESSERACT_ROOT/usr/share/tessdata" \
    "/usr/share/tesseract-ocr/5/tessdata" \
    "/usr/share/tesseract-ocr/4.00/tessdata" \
    "/usr/share/tessdata"; do
    if [ -d "$tessdata_dir" ]; then
      export TESSDATA_PREFIX="$tessdata_dir"
      break
    fi
  done
fi

if command -v tesseract >/dev/null 2>&1; then
  tesseract --version
  tesseract --list-langs || true
fi

exec gunicorn render_app:app --bind "0.0.0.0:${PORT:-10000}" --timeout 300
