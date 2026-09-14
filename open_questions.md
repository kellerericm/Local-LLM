# Open questions

## Even longer-term memory
There is probably some structure that can be added to the LLM, or modified inside it, to extend its memory. The analogy is a human's long-term memory versus short-term memory (like context). Its form is unknown and hasn't been researched yet.

Directions to look into (unevaluated):
- Retrieval over a persistent notes store (RAG). This is the cheapest option and needs no model changes.
- Learned memory tokens or a compressed summary state carried between sessions.
- Per-project LoRA adapters trained on accumulated notes. This ties into the specialist-model systems (Phase 4).
- Architectural memory modules (memory layers or key-value stores attached to the model's internals).

## Tool-call reliability of small local models
How much coordinator scaffolding (argument repair, retries, constrained decoding) is needed before an 8–14B model follows multi-step task lists reliably? The model benchmark should answer this.

## Sandbox strength
The command policy is pattern-based and best-effort. Is a stronger isolation layer worth it on Windows (Windows Sandbox, a restricted token, a separate low-privilege user account)?
