"""Regenerate packages/contracts/{schemas/*.json,openapi.json} from the Python models.

uv run python scripts/gen_contracts.py          # write files
uv run python scripts/gen_contracts.py --check  # exit 1 if files are stale
"""

import json
import sys
from pathlib import Path

from office_api.main import create_app
from office_contracts import SCHEMA_MODELS

ROOT = Path(__file__).resolve().parents[1] / "packages" / "contracts"


def render() -> dict[Path, str]:
    out: dict[Path, str] = {}
    for name, model in SCHEMA_MODELS.items():
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"https://ai-project-office.local/schemas/{name}.schema.json",
            **model.model_json_schema(),
        }
        out[ROOT / "schemas" / f"{name}.schema.json"] = json.dumps(schema, indent=2) + "\n"
    out[ROOT / "openapi.json"] = json.dumps(create_app().openapi(), indent=2) + "\n"
    return out


def main() -> int:
    check = "--check" in sys.argv
    stale = []
    for path, content in render().items():
        if path.exists() and path.read_text() == content:
            continue
        stale.append(path)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    for path in stale:
        print(("stale: " if check else "wrote: ") + str(path.relative_to(ROOT.parents[1])))
    return 1 if check and stale else 0


if __name__ == "__main__":
    sys.exit(main())
