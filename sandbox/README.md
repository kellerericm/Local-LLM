# Sandbox

Small, self-contained workloads for the agent to practice on and for testing long-running tasks.
Everything here is **fictional** and safe to modify. Point a LocalAgent project at one of the subfolders, not at `sandbox/` itself, so the agent can't see the answer keys.

| Folder | Use case | What the agent does | How it's checked |
|---|---|---|---|
| `lake_veyra/` | Search documents → notes → report | Answer "How has phosphorus pollution in Lake Veyra changed since 2019, what drives it, and what is being done?" from 8 documents. One is irrelevant; two disagree. | `answer_keys/lake_veyra.json`: required facts, the contradiction, and quotes that citations must match |
| `tune_me/` | Auto-research improvement loop | Improve `model.py` so `python evaluate.py` reports a lower error. | The evaluator's score (baseline 3.01; true function 0.29), **plus** the hidden generalization check `answer_keys/tune_me_holdout.py <model.py>`, which scores unseen points inside and beyond the data range. Low visible score with high extrapolation error = overfitting |

Reset a workload with `git checkout -- sandbox/<folder>`.
