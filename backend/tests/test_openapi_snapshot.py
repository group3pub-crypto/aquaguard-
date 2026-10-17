"""backend/openapi.json is the contract snapshot copied into dashboard/bot contract tests."""

import json
from pathlib import Path

from app.main import create_app
from tests.conftest import make_settings

SNAPSHOT = Path(__file__).resolve().parents[1] / "openapi.json"


def test_openapi_snapshot_is_current(tmp_path):
    spec = create_app(make_settings(tmp_path)).openapi()
    current = json.loads(SNAPSHOT.read_text())
    assert current == json.loads(json.dumps(spec)), (
        "backend/openapi.json is stale: regenerate with\n"
        '  ENVIRONMENT=test python -c "import json;from app.main import app_factory;'
        'print(json.dumps(app_factory().openapi(),indent=2,sort_keys=True))" > openapi.json'
    )
