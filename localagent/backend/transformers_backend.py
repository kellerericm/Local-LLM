"""Hugging Face Transformers backend. Runs inside the model worker process (see worker.py)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Iterator


@dataclass
class Control:
    pause: "threading.Event"        # multiprocessing.Event in the worker
    cancel: "threading.Event"


class TransformersBackend:
    def __init__(self, model_id: str, quantization: str = "4bit", max_vram_gb: float = 14.0,
                 cpu_threads: int = 8, cache_dir: str | None = None, offload: str = "auto",
                 max_cpu_ram_gb: float = 12.0):
        self.model_id = model_id
        self.quantization = quantization
        self.max_vram_gb = max_vram_gb
        self.cpu_threads = cpu_threads
        self.cache_dir = cache_dir
        self.offload = offload
        self.max_cpu_ram_gb = max_cpu_ram_gb
        self.model = None
        self.tokenizer = None
        self.placement: dict = {}
        self._adapters: set[str] = set()
        self._active_adapter: str | None = None

    def load(self) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

        torch.set_num_threads(max(1, int(self.cpu_threads)))
        quant = None
        if self.quantization == "4bit":
            quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
                                       bnb_4bit_compute_dtype=torch.bfloat16)
        elif self.quantization == "8bit":
            quant = BitsAndBytesConfig(load_in_8bit=True)
        from transformers import AutoConfig

        config = AutoConfig.from_pretrained(self.model_id, cache_dir=self.cache_dir)
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_id, cache_dir=self.cache_dir)
        kwargs = dict(cache_dir=self.cache_dir, dtype=torch.bfloat16)
        if self.offload == "gpu_only":
            # Everything on GPU 0; fails with out-of-memory rather than silently running slow.
            kwargs.update(device_map={"": 0})
        else:
            # GPU first up to the VRAM cap, the rest in system RAM (much slower for those layers).
            kwargs.update(device_map="auto",
                          max_memory={0: f"{self.max_vram_gb}GiB", "cpu": f"{self.max_cpu_ram_gb}GiB"})
        # Pre-quantized checkpoints carry their own quantization config.
        if quant is not None and getattr(config, "quantization_config", None) is None:
            if self._is_multimodal(config):
                # Keep the vision tower unquantized; only text layers get 4/8-bit.
                quant.llm_int8_skip_modules = ["visual", "vision_tower", "lm_head"]
            if self.offload != "gpu_only":
                quant.llm_int8_enable_fp32_cpu_offload = True   # required for quantized models to spill to RAM
            kwargs["quantization_config"] = quant
        if self._is_multimodal(config):
            from transformers import AutoModelForMultimodalLM
            self.model = AutoModelForMultimodalLM.from_pretrained(self.model_id, **kwargs)
        else:
            self.model = AutoModelForCausalLM.from_pretrained(self.model_id, **kwargs)
        self.model.eval()
        self.stop_token_ids = self._stop_tokens()
        self.placement = self._placement()

    def _placement(self) -> dict:
        """Where the model's parts ended up, so the UI can warn when layers were offloaded to RAM."""
        # transformers 5 no longer sets hf_device_map, so measure parameter bytes per device.
        size = {"gpu": 0, "cpu": 0, "meta": 0}
        for p in self.model.parameters():
            kind = "gpu" if p.device.type == "cuda" else "meta" if p.device.type == "meta" else "cpu"
            size[kind] += p.numel() * p.element_size()
        total = sum(size.values()) or 1
        off = size["cpu"] + size["meta"]
        return {"gpu_gb": round(size["gpu"] / 2**30, 2), "cpu_gb": round(off / 2**30, 2),
                "offloaded_pct": round(100 * off / total), "offloaded": off > 0, "offload_mode": self.offload}

    def _stop_tokens(self) -> list[int]:
        """End-of-turn tokens. Some checkpoints (e.g. Qwen3.5) ship no generation_config, so the default
        eos is <|endoftext|> and generation runs past <|im_end|>, inventing further turns."""
        ids: list[int] = []
        gen_eos = getattr(getattr(self.model, "generation_config", None), "eos_token_id", None)
        for value in (gen_eos, self.tokenizer.eos_token_id):
            ids.extend(value if isinstance(value, list) else [value] if value is not None else [])
        unk = self.tokenizer.unk_token_id
        for tok in ("<|im_end|>", "<|endoftext|>", "<|eot_id|>", "<end_of_turn>"):
            tid = self.tokenizer.convert_tokens_to_ids(tok)
            if isinstance(tid, int) and tid >= 0 and tid != unk:
                ids.append(tid)
        return list(dict.fromkeys(ids))

    @staticmethod
    def _is_multimodal(config) -> bool:
        return hasattr(config, "vision_config") or any(
            "ConditionalGeneration" in a for a in (getattr(config, "architectures", None) or []))

    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer(text, add_special_tokens=False)["input_ids"])

    def _apply_adapter(self, adapter: dict | None) -> None:
        name = adapter["name"] if adapter else None
        if name == self._active_adapter:
            return
        if name is None:
            if self._adapters:
                self.model.disable_adapters()
        else:
            if name not in self._adapters:
                self.model.load_adapter(adapter["path"], adapter_name=name)
                self._adapters.add(name)
            self.model.set_adapter(name)
            self.model.enable_adapters()
        self._active_adapter = name

    def _think_end_id(self) -> int | None:
        tid = self.tokenizer.convert_tokens_to_ids("</think>")
        return tid if isinstance(tid, int) and tid >= 0 and tid != self.tokenizer.unk_token_id else None

    def generate(self, messages: list[dict], tools: list[dict] | None, params: dict,
                 adapter: dict | None = None, control: Control | None = None) -> Iterator[str | dict]:
        """Yields text chunks, then one {"usage": {...}} dict."""
        import torch
        from transformers import (LogitsProcessor, LogitsProcessorList, StoppingCriteria, StoppingCriteriaList,
                                  TextIteratorStreamer)

        self._apply_adapter(adapter)
        thinking = bool(params.get("thinking", True))
        enc = self.tokenizer.apply_chat_template(
            messages, tools=tools or None, add_generation_prompt=True, return_dict=True, return_tensors="pt",
            enable_thinking=thinking)
        enc = {k: v.to(self.model.device) for k, v in enc.items()}
        prompt_len = int(enc["input_ids"].shape[1])
        think_end = self._think_end_id()
        budget_state = {"hit": False}
        processors = []

        presence = float(params.get("presence_penalty", 0.0) or 0.0)
        if presence > 0:
            class PresencePenalty(LogitsProcessor):
                """Subtract a flat penalty from every token already used in this reply (not the prompt)."""
                def __call__(self, input_ids, scores):
                    generated = input_ids[:, prompt_len:]
                    if generated.shape[1]:
                        seen = torch.zeros_like(scores, dtype=torch.bool).scatter_(1, generated, True)
                        scores = scores - presence * seen.to(scores.dtype)
                    return scores
            processors.append(PresencePenalty())

        budget = int(params.get("thinking_budget", 0) or 0)
        if thinking and budget > 0 and think_end is not None:
            wrap_ids = self.tokenizer.encode(
                "\n\nI'm at my thinking budget, so I'll stop reasoning here and act on what I have.\n",
                add_special_tokens=False) + [think_end]

            class ThinkingBudget(LogitsProcessor):
                """Once the budget is spent, force a short wrap-up sentence and </think>."""
                def __call__(self, input_ids, scores):
                    generated = input_ids[0, prompt_len:]
                    if (generated == think_end).any():
                        return scores
                    over = generated.shape[0] - budget
                    if 0 <= over < len(wrap_ids):
                        budget_state["hit"] = True
                        forced = torch.full_like(scores, float("-inf"))
                        forced[:, wrap_ids[over]] = 0.0
                        return forced
                    return scores
            processors.append(ThinkingBudget())

        class ControlCriteria(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs):
                stop = False
                if control is not None:
                    while control.pause.is_set() and not control.cancel.is_set():
                        time.sleep(0.25)        # yield the GPU to whatever else is running
                    stop = control.cancel.is_set()
                return torch.full((input_ids.shape[0],), stop, dtype=torch.bool, device=input_ids.device)

        streamer = TextIteratorStreamer(self.tokenizer, skip_prompt=True, skip_special_tokens=True)
        gen_kwargs = dict(**enc, streamer=streamer, max_new_tokens=int(params.get("max_new_tokens", 4096)),
                          stopping_criteria=StoppingCriteriaList([ControlCriteria()]),
                          eos_token_id=self.stop_token_ids,
                          pad_token_id=self.tokenizer.pad_token_id if self.tokenizer.pad_token_id is not None
                          else self.stop_token_ids[0])
        if processors:
            gen_kwargs["logits_processor"] = LogitsProcessorList(processors)
        repetition = float(params.get("repetition_penalty", 1.0) or 1.0)
        if repetition != 1.0:
            gen_kwargs["repetition_penalty"] = repetition
        temperature = float(params.get("temperature", 0.6))
        if temperature > 0:
            gen_kwargs.update(do_sample=True, temperature=temperature, top_p=float(params.get("top_p", 0.95)),
                              top_k=int(params.get("top_k", 20)))
            if float(params.get("min_p", 0.0) or 0.0) > 0:
                gen_kwargs["min_p"] = float(params["min_p"])
        else:
            gen_kwargs.update(do_sample=False)

        errors: list[BaseException] = []
        outputs: list = []

        def run():
            try:
                with torch.inference_mode():
                    outputs.append(self.model.generate(**gen_kwargs))
            except BaseException as e:      # surface in the consumer thread
                errors.append(e)
                streamer.end()

        started = time.time()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        for text in streamer:
            if text:
                yield text
        thread.join()
        if errors:
            raise errors[0]
        elapsed = time.time() - started
        generated = outputs[0][0, prompt_len:].tolist() if outputs else []
        thinking_tokens = 0
        if thinking:
            thinking_tokens = generated.index(think_end) + 1 if think_end in generated else len(generated)
        yield {"usage": {
            "prompt_tokens": prompt_len, "completion_tokens": len(generated), "thinking_tokens": thinking_tokens,
            "answer_tokens": len(generated) - thinking_tokens, "seconds": round(elapsed, 2),
            "tokens_per_s": round(len(generated) / elapsed, 1) if elapsed > 0 else None,
            "thinking_budget_hit": budget_state["hit"]}}
