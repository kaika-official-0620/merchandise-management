#!/usr/bin/env bash
set -euo pipefail

# Persistent disks are mounted only at runtime, so validation and seeding run
# inside staging_app before Gunicorn exposes its first worker to traffic.
TESSERACT_ROOT="${RENDER_TESSERACT_ROOT:-$PWD/.render/tesseract}"
if [ -d "$TESSERACT_ROOT/usr/bin" ]; then
  export PATH="$TESSERACT_ROOT/usr/bin:$PATH"
fi
if [ -d "$TESSERACT_ROOT/usr/lib/x86_64-linux-gnu" ]; then
  export LD_LIBRARY_PATH="$TESSERACT_ROOT/usr/lib/x86_64-linux-gnu:$TESSERACT_ROOT/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
fi
if [ -z "${TESSDATA_PREFIX:-}" ]; then
  for candidate in "$TESSERACT_ROOT/usr/share/tesseract-ocr/5/tessdata" "$TESSERACT_ROOT/usr/share/tesseract-ocr/4.00/tessdata" "$TESSERACT_ROOT/usr/share/tessdata"; do
    if [ -d "$candidate" ]; then
      export TESSDATA_PREFIX="$candidate"
      break
    fi
  done
fi
exec gunicorn staging_app:app --workers 1 --bind "0.0.0.0:${PORT:-10000}" --timeout 300
