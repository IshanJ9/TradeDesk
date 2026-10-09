"""Regenerate frontend/src/lib/types.gen.ts from the backend's OpenAPI schema.

    python scripts/gen_types.py

Pydantic models are the single source of truth. Never edit types.gen.ts by hand.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


def main() -> int:
    app = create_app(Settings(ticker_interval=None))
    spec_path = FRONTEND / "openapi.json"
    spec_path.write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {spec_path.relative_to(ROOT)}")

    (FRONTEND / "src" / "lib").mkdir(parents=True, exist_ok=True)
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        print("npm not found; install Node to generate the TypeScript types", file=sys.stderr)
        return 1
    if not (FRONTEND / "node_modules" / ".bin").exists():
        subprocess.run([npm, "install"], cwd=FRONTEND, check=True)
    subprocess.run([npm, "run", "gen:types"], cwd=FRONTEND, check=True)
    print("wrote frontend/src/lib/types.gen.ts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
