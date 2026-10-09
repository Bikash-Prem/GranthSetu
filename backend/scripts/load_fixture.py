"""Load the clearly-labelled TEST FIXTURE into a database and broadcast index_updated.
Used only for offline integration testing. Never use it for demos or evaluation."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from granthsetu.bus import get_bus  # noqa: E402
from granthsetu.db import get_db  # noqa: E402
from granthsetu.ingest import ingest_records  # noqa: E402

recs = json.loads((Path(__file__).resolve().parents[1] / "tests/fixtures/records.json").read_text())["records"]
stored = ingest_records(recs)
v = get_db().bump_index_version()
get_bus().publish({"type": "index_updated", "version": v, "added": len(stored), "resources": stored})
print("loaded", len(stored), "fixture records, index version", v)
get_db().close()
