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
                 cpu_threads: int = 8, cache_dir: str | None = None):
        self.model_id = model_id
        self.quantization = quantization
        self.max_vram_gb = max_vram_gb
        self.cpu_threads = cpu_threads
        self.cache_dir = cache_dir
        self.model = None
        self.tokenizer = None
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
        kwargs = dict(cache_dir=self.cache_dir, device_map="auto", dtype=torch.bfloat16,
                      max_memory={0: f"{self.max_vram_gb}GiB", "cpu": "24GiB"})
        # Pre-quantized checkpoints carry their own quantization config.
        if quant is not None and getattr(config, "quantization_config", None) is None:
            if self._is_multimodal(config):
                # Keep the vision tower unquantized; only text layers get 4/8-bit.
                quant.llm_int8_skip_modules = ["visual", "vision_tower", "lm_head"]
            kwargs["quantization_config"] = quant
        if self._is_multimodal(config):
            from transformers import AutoModelForMultimodalLM
            self.model = AutoModelForMultimodalLM.from_pretrained(self.model_id, **kwargs)
        else:
            self.model = AutoModelForCausalLM.from_pretrained(self.model_id, **kwargs)
        self.model.eval()

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

    def generate(self, messages: list[dict], tools: list[dict] | None, params: dict,
                 adapter: dict | None = None, control: Control | None = None) -> Iterator[str]:
        import torch
        from transformers import StoppingCriteria, StoppingCriteriaList, TextIteratorStreamer

        self._apply_adapter(adapter)
        enc = self.tokenizer.apply_chat_template(
            messages, tools=tools or None, add_generation_prompt=True, return_dict=True, return_tensors="pt",
            enable_thinking=bool(params.get("thinking", True)))
        enc = {k: v.to(self.model.device) for k, v in enc.items()}

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
                          stopping_criteria=StoppingCriteriaList([ControlCriteria()]))
        temperature = float(params.get("temperature", 0.6))
        if temperature > 0:
            gen_kwargs.update(do_sample=True, temperature=temperature, top_p=float(params.get("top_p", 0.95)),
                              top_k=int(params.get("top_k", 20)))
        else:
            gen_kwargs.update(do_sample=False)

        errors: list[BaseException] = []

        def run():
            try:
                with torch.inference_mode():
                    self.model.generate(**gen_kwargs)
            except BaseException as e:      # surface in the consumer thread
                errors.append(e)
                streamer.end()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        for text in streamer:
            if text:
                yield text
        thread.join()
        if errors:
            raise errors[0]
