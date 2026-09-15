"""Set up data for sandbox/lm_speedrun (run once; needs network for the download).

    python -m bench.datasets.prepare_lm_speedrun [--split 80/20 | --split 80/10/10]

- Downloads Shakespeare's complete works (Project Gutenberg #100, public domain), strips the Gutenberg header/footer.
- Splits into contiguous chunks (no leakage between splits):
    train (visible to the agent)  -> sandbox/lm_speedrun/data/train.bin
    test  (hidden)                -> D:\\LocalAgent\\datasets\\lm_speedrun\\test.bin
    val   (hidden, 3-way only)    -> D:\\LocalAgent\\datasets\\lm_speedrun\\val.bin
Default split 80/20 train/test (design decision 7). 80/10/10 adds a validation set for keep/revert decisions on long
or indefinite runs, with test scored once at the end.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_DATA = ROOT / "sandbox" / "lm_speedrun" / "data"
HIDDEN = Path(r"D:\LocalAgent\datasets\lm_speedrun")
URL = "https://www.gutenberg.org/cache/epub/100/pg100.txt"


def fetch() -> bytes:
    cache = HIDDEN / "pg100.txt"
    if cache.exists():
        return cache.read_bytes()
    HIDDEN.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(URL, headers={"User-Agent": "LocalAgent-sandbox/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    cache.write_bytes(raw)
    return raw


def strip_gutenberg(text: str) -> str:
    start = re.search(r"\*\*\* ?START OF (THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\n", text)
    end = re.search(r"\*\*\* ?END OF (THE|THIS) PROJECT GUTENBERG EBOOK", text)
    body = text[start.end() if start else 0: end.start() if end else len(text)]
    return body.replace("\r\n", "\n").strip() + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="80/20", choices=["80/20", "80/10/10"])
    args = ap.parse_args()
    data = strip_gutenberg(fetch().decode("utf-8", errors="replace")).encode("utf-8")
    n = len(data)
    parts = [int(p) for p in args.split.split("/")]
    cut1 = n * parts[0] // 100
    WORKSPACE_DATA.mkdir(parents=True, exist_ok=True)
    HIDDEN.mkdir(parents=True, exist_ok=True)
    (WORKSPACE_DATA / "train.bin").write_bytes(data[:cut1])
    for stale in ("val.bin", "test.bin"):
        (HIDDEN / stale).unlink(missing_ok=True)
    if len(parts) == 2:
        (HIDDEN / "test.bin").write_bytes(data[cut1:])
    else:
        cut2 = cut1 + n * parts[1] // 100
        (HIDDEN / "val.bin").write_bytes(data[cut1:cut2])
        (HIDDEN / "test.bin").write_bytes(data[cut2:])
    manifest = {"source": URL, "split": args.split, "total_bytes": n,
                "files": {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()[:16]}
                          for p in [WORKSPACE_DATA / "train.bin", *sorted(HIDDEN.glob("*.bin"))]}}
    (HIDDEN / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
