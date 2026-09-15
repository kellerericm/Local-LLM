# lm_speedrun

Train the best small language model you can in **5 minutes** of training on this computer's GPU.

The model reads raw bytes of English text (Shakespeare's complete works) and predicts the next byte.
**Score: bits per byte (bpb)** on held-out text you can't see. **Lower is better.**
A model that knows nothing scores 8.0; good small models on this data reach about 1.5–2.0.

## Files
| File | What it is | Can you change it? |
|---|---|---|
| `train.py` | Model, optimizer, and training loop | **Yes: this is what you improve** |
| `dev_eval.py` | Scores `model.pt` on a dev slice of the training data | No |
| `data/train.bin` | Training text (raw bytes) | No |
| `README.md` | These rules | No |

## Rules
- Only change `train.py`, and keep its interface working (see the docstring at the top of `train.py`).
- Training must finish within the time budget (`TIME_BUDGET_S`, 300 seconds). Official runs stop it if it doesn't.
- GPU memory is capped at about **3 GB** during official runs, because the GPU is shared with the agent's own model.
- Use PyTorch and the Python standard library only. No downloads, no pretrained weights.
- The dev slice is the last 5% of `data/train.bin`. The model also trains on it by default; you may exclude it from training if you want a cleaner dev signal.

## Try it
```
python dev_eval.py --train
```
For quick checks, set a shorter budget first, e.g. in PowerShell: `$env:TIME_BUDGET_S = "60"`.
