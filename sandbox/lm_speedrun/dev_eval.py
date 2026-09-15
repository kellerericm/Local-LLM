"""Score model.pt on a dev slice (the last 5% of data/train.bin). Prints `dev_bpb: <number>` (lower is better).

This is for your own experiments. The official score uses data you can't see, so tuning hard against this dev slice
can mislead you; prefer changes that should help in general.

    python dev_eval.py            # score an existing model.pt
    python dev_eval.py --train    # run train.py first (respects TIME_BUDGET_S), then score
"""
import math
import subprocess
import sys
from pathlib import Path

import torch
import torch.nn.functional as F

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def bits_per_byte(model, data: torch.Tensor, block: int, device: str, max_bytes: int = 200_000) -> float:
    data = data[:max_bytes]
    total, count = 0.0, 0
    with torch.no_grad():
        for i in range(0, len(data) - 1, block):
            x = data[i:i + block].unsqueeze(0).to(device)
            y = data[i + 1:i + block + 1].unsqueeze(0).to(device)
            x = x[:, :y.size(1)]
            if y.numel() == 0:
                break
            logits = model(x)
            total += F.cross_entropy(logits.reshape(-1, 256), y.reshape(-1), reduction="sum").item()
            count += y.numel()
    return total / count / math.log(2)


def main():
    if "--train" in sys.argv:
        subprocess.run([sys.executable, str(HERE / "train.py")], check=True, cwd=HERE)
    import train
    device = "cuda" if torch.cuda.is_available() else "cpu"
    raw = torch.frombuffer(bytearray((HERE / "data" / "train.bin").read_bytes()), dtype=torch.uint8).long()
    dev = raw[int(len(raw) * 0.95):]
    model = train.load_model(HERE / "model.pt").to(device)
    print(f"dev_bpb: {bits_per_byte(model, dev, train.BLOCK_SIZE, device):.4f}")


if __name__ == "__main__":
    main()
