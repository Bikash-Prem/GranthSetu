#!/usr/bin/env python3
"""GranthSetu search tool for agents (stdlib only).
usage: search.py "question"            -> agent search, prints compact JSON
       search.py --fetch "topic"       -> queue a live ingestion job
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("GRANTHSETU_URL", "http://localhost:8080").rstrip("/")


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"GranthSetu error {e.code}: {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach GranthSetu at {BASE}: {e.reason}")


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if args[0] == "--fetch":
        print(json.dumps(post("/api/ingest/topic", {"topic": " ".join(args[1:])})))
        return
    r = post("/api/search", {"query": " ".join(args), "mode": "agent", "explain": True})
    out = {
        "confidence": r["confidence"],
        "decision": r["decision"],
        "language": r["language"],
        "explanation": [
            {"text": s["display_text"], "source_rank": next((x["rank"] for x in r["results"] if x["passage_id"] == s["pid"]), None)}
            for s in r["explanation"]["sentences"] if s["verified"]
        ],
        "results": [
            {k: x[k] for k in ("rank", "title", "url", "lang", "source", "licence", "reason")} for x in r["results"]
        ],
        "restricted": [
            {k: x[k] for k in ("title", "url", "access", "source", "licence")} for x in r.get("restricted_results", [])
        ],
        "gap_board": r.get("gap"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
