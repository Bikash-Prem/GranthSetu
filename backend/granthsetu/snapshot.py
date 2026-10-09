"""Export / import a snapshot of the live library (real, already-ingested records).

Use it as a venue-wifi backup: after a successful live seed, run
`python -m granthsetu.snapshot export data/snapshot.jsonl.gz` and commit it.
Import re-embeds passages with the current embedder.
"""
from __future__ import annotations

import gzip
import json
import sys

from .bus import get_bus
from .db import get_db
from .embeddings import get_embedder, to_bytes

FIELDS = ["key", "topic_key", "title", "source", "kind", "url", "lang", "licence", "licence_url", "attribution", "author", "subject"]


def export(path: str) -> int:
    db = get_db()
    n = 0
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in db.query("SELECT * FROM resources ORDER BY id", primary=True):
            ps = db.query("SELECT text FROM passages WHERE resource_id=%s ORDER BY chunk_no", (r["id"],), primary=True)
            f.write(json.dumps({**{k: r[k] for k in FIELDS}, "passages": [p["text"] for p in ps]}, ensure_ascii=False) + "\n")
            n += 1
    return n


def import_(path: str) -> int:
    db, emb = get_db(), get_embedder()
    n = 0
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            texts = rec.pop("passages")
            vecs = emb.embed([f'{rec["title"]}. {t}' for t in texts], kind="doc") if texts else []
            db.upsert_resource(rec, [(t, to_bytes(v), emb.name) for t, v in zip(texts, vecs)])
            n += 1
    v = db.bump_index_version()
    get_bus().publish({"type": "index_updated", "version": v, "added": n, "snapshot": True})
    return n


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("export", "import"):
        sys.exit("usage: python -m granthsetu.snapshot export|import <file.jsonl.gz>")
    count = export(sys.argv[2]) if sys.argv[1] == "export" else import_(sys.argv[2])
    print(f"{sys.argv[1]}ed {count} resources")
    get_db().close()
