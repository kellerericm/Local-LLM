# tune_me

`data.csv` holds measurements of a sensor response `y` at settings `x` (0–10).
`model.py` defines `predict(x)`, which estimates `y`.

**Goal:** make predictions more accurate.

```
python evaluate.py
```
It prints `score: <number>`: the root-mean-square error on the data. **Lower is better.**

Rules:
- Only change `model.py`. Don't edit `evaluate.py` or `data.csv`.
- Use only the Python standard library (`math`, etc.).
- `predict` must run quickly: the evaluator calls it 200 times.
