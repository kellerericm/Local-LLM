"""Score model.predict against data.csv. Prints `score: <rmse>` (lower is better)."""
import csv
import importlib
import math
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here))
model = importlib.import_module("model")

with open(here / "data.csv", newline="") as f:
    rows = [(float(r["x"]), float(r["y"])) for r in csv.DictReader(f)]

errors = []
for x, y in rows:
    try:
        errors.append((model.predict(x) - y) ** 2)
    except Exception as e:  # a broken model scores as a failure, not a crash
        print(f"error: predict({x}) raised {type(e).__name__}: {e}")
        print("score: inf")
        sys.exit(1)

print(f"score: {math.sqrt(sum(errors) / len(errors)):.4f}")
