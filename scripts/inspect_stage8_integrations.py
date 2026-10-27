"""Print redacted readiness inventory; never loads .env, app, DB or provider SDK."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stage8_integrations_readiness import inspect_integrations


if __name__ == '__main__':
    print(json.dumps(inspect_integrations(os.environ), ensure_ascii=False, indent=2))
