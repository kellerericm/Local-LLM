"""Train a small byte-level language model within a fixed time budget.

You may change anything in this file (architecture, optimizer, schedule, batch size, initialization, data sampling,
precision, ...) as long as the interface below keeps working:

  - `python train.py` trains for at most TIME_BUDGET_S seconds (environment variable, default 300), then saves
    the model to model.pt and exits.
  - `load_model(path)` returns a torch.nn.Module in eval mode whose forward(x) takes a LongTensor of bytes
    [batch, time] (values 0-255, time <= BLOCK_SIZE) and returns next-byte logits [batch, time, 256].
  - BLOCK_SIZE is the longest context the model accepts.
  - Importing this file must not start training.
"""
import math
import os
import time
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F

HERE = Path(__file__).resolve().parent
BLOCK_SIZE = 256
CONFIG = dict(n_layer=4, n_head=4, n_embd=256, dropout=0.0)


class Block(nn.Module):
    def __init__(self, n_embd, n_head, dropout):
        super().__init__()
        self.ln1 = nn.LayerNorm(n_embd)
        self.attn = nn.MultiheadAttention(n_embd, n_head, dropout=dropout, batch_first=True)
        self.ln2 = nn.LayerNorm(n_embd)
        self.mlp = nn.Sequential(nn.Linear(n_embd, 4 * n_embd), nn.GELU(), nn.Linear(4 * n_embd, n_embd))

    def forward(self, x):
        t = x.size(1)
        mask = torch.triu(torch.ones(t, t, dtype=torch.bool, device=x.device), diagonal=1)
        h = self.ln1(x)
        x = x + self.attn(h, h, h, attn_mask=mask, need_weights=False)[0]
        return x + self.mlp(self.ln2(x))


class ByteGPT(nn.Module):
    def __init__(self, n_layer, n_head, n_embd, dropout):
        super().__init__()
        self.tok = nn.Embedding(256, n_embd)
        self.pos = nn.Embedding(BLOCK_SIZE, n_embd)
        self.blocks = nn.ModuleList(Block(n_embd, n_head, dropout) for _ in range(n_layer))
        self.ln_f = nn.LayerNorm(n_embd)
        self.head = nn.Linear(n_embd, 256, bias=False)

    def forward(self, idx):
        pos = torch.arange(idx.size(1), device=idx.device)
        x = self.tok(idx) + self.pos(pos)
        for block in self.blocks:
            x = block(x)
        return self.head(self.ln_f(x))


def load_model(path):
    model = ByteGPT(**CONFIG)
    model.load_state_dict(torch.load(path, map_location="cpu"))
    return model.eval()


def main():
    budget = float(os.environ.get("TIME_BUDGET_S", "300"))
    start = time.time()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    torch.manual_seed(0)
    data = torch.frombuffer(bytearray((HERE / "data" / "train.bin").read_bytes()), dtype=torch.uint8).long()
    model = ByteGPT(**CONFIG).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=0.1)
    batch_size = 32
    est_steps = 3000                      # the schedule assumes roughly this many steps fit in the budget
    step = 0
    while time.time() - start < budget - 15:    # leave time to save
        lr = 1e-3 * 0.5 * (1 + math.cos(math.pi * min(step / est_steps, 1.0)))
        for g in opt.param_groups:
            g["lr"] = lr
        ix = torch.randint(len(data) - BLOCK_SIZE - 1, (batch_size,))
        x = torch.stack([data[i:i + BLOCK_SIZE] for i in ix]).to(device)
        y = torch.stack([data[i + 1:i + BLOCK_SIZE + 1] for i in ix]).to(device)
        loss = F.cross_entropy(model(x).view(-1, 256), y.view(-1))
        opt.zero_grad(set_to_none=True)
        loss.backward()
        opt.step()
        if step % 200 == 0:
            print(f"step {step} loss {loss.item():.4f} bpb {loss.item() / math.log(2):.4f} "
                  f"elapsed {time.time() - start:.0f}s", flush=True)
        step += 1
    torch.save(model.state_dict(), HERE / "model.pt")
    print(f"done: {step} steps in {time.time() - start:.0f}s", flush=True)


if __name__ == "__main__":
    main()
