"""Idempotent staging bootstrap; initial passwords come only from secret env.

Run in the staging Render service Shell. No reset or password printing exists.
Ordinary restarts run the same bootstrap automatically.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    from staging_environment import StagingConfigurationError
    try:
        import staging_app  # Validates marker/disk and adds only absent seeds.
    except StagingConfigurationError as error:
        print("Staging bootstrap refused: " + str(error), file=sys.stderr)
        raise SystemExit(1)
    print("Staging test accounts are ready. Existing test work and passwords were retained.")
