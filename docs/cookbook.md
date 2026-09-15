# LocalAgent install cookbook

Follow these steps in order. Each step says what to do, how to check that it worked, and what to try if it didn't.

## What you need
- Windows 10/11 with an NVIDIA GPU. 12 GB+ VRAM is recommended; this was built on an RTX 4060 Ti 16 GB.
- An up-to-date NVIDIA driver. Check with `nvidia-smi` in a terminal.
- [Anaconda or Miniconda](https://www.anaconda.com/download).
- About 60 GB free on a drive for models. The defaults use `D:\LocalAgent`.

## 1. Get the code
Put the project folder somewhere, e.g. `C:\Users\<you>\Documents\Projects\Local LLM and Memory Lenthening`.
All commands below are run from that folder in an **Anaconda Prompt** or PowerShell.

## 2. Create the Python environment
```powershell
conda create -n local-llm python=3.14 -y
conda activate local-llm
```
**Check:** `python --version` prints 3.14.x.
**If a later step fails on 3.14** (a package has no wheel), create the env with `python=3.12` instead. Everything here also works on 3.12.

## 3. Install PyTorch with CUDA
Pick the CUDA build that matches your driver. `nvidia-smi` shows the highest CUDA version the driver supports; cu130 needs a CUDA 13 driver.
```powershell
python -m pip install torch --index-url https://download.pytorch.org/whl/cu130
```
**Check:**
```powershell
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```
It should print `True` and your GPU's name. If it prints `False`, you probably installed the CPU build. Reinstall with the `--index-url` above.

## 4. Install the rest
```powershell
python -m pip install -r requirements.txt
```
**Check:** `python -m pytest -q tests` should report everything passing. These tests don't need the GPU or a model.

## 5. Choose where models and data live (optional)
Defaults:
- models: `D:\LocalAgent\models`
- chats and settings: `D:\LocalAgent\data`

To keep data somewhere else, set an environment variable before starting, e.g. `$env:LOCALAGENT_DATA_DIR = "E:\LocalAgent\data"`, or pass `--data-dir`. You can change the models folder later in Settings.

> On Windows, Hugging Face's cache wants symlinks. Without Windows Developer Mode it prints a warning and copies files instead, which uses somewhat more disk. Turning on Developer Mode is optional.

## 6. Try the app without a model
```powershell
python -m localagent --fake-model
```
Your browser opens at http://127.0.0.1:8765. The fake model echoes you. Type `/ls`, `/tasks` or `/outside` to see tool calls, the task list, and an approval prompt.
Stop the server with **Ctrl+C**.

## 7. Run with a real model
```powershell
python -m localagent
```
- The first message you send downloads the model set in Settings. The default is `Qwen/Qwen3.5-9B`, about 19 GB, chosen by benchmark (see `experiments.md`). Then it loads (about a minute), and later messages are quick.
- While loading, 4-bit quantization briefly uses about 19 GB of system RAM. Close memory-heavy apps first. Pre-quantized checkpoints (repos with `bnb-4bit` in the name) avoid this spike.
- To download ahead of time:
  ```powershell
  python -c "from huggingface_hub import snapshot_download as d; d('Qwen/Qwen3.5-9B', cache_dir=r'D:\LocalAgent\models')"
  ```
- **Recommended once:** save a pre-quantized copy, so every later load takes seconds and skips the RAM spike:
  ```powershell
  python -m localagent.backend.quantize_copy --model Qwen/Qwen3.5-9B --out "D:\LocalAgent\models\local\Qwen3.5-9B-bnb-4bit"
  ```
  It checks that the copy gives identical answers (see `verify.json` in that folder). LocalAgent then uses the copy automatically whenever the model is `Qwen/Qwen3.5-9B` with 4-bit quantization.
- To pick a model based on evidence, see `experiments.md` and run the benchmark (step 9).

## 8. Open it from a shortcut (optional)
Create `LocalAgent.bat` on your desktop:
```bat
@echo off
call "%USERPROFILE%\anaconda3\Scripts\activate.bat" local-llm
cd /d "C:\Users\<you>\Documents\Projects\Local LLM and Memory Lenthening"
python -m localagent
```

## 9. Benchmark models (optional)
```powershell
python -m bench.run --model Qwen/Qwen3.5-9B
```
Results are saved to `D:\LocalAgent\bench-runs\<date>_<model>\summary.md`. Each run takes roughly 10–40 minutes.
To compare, pick a model from a **different family** (e.g. a small Gemma or Llama instruct model) rather than another size of the same one. Delete candidates you don't keep: they're large.

## Troubleshooting
| Symptom | Try |
|---|---|
| "Failed to load model" with out-of-memory | Lower **VRAM limit** in Settings, choose `4bit`, or a smaller model. Close GPU-heavy apps. |
| Status says "Paused: GPU busy" and never resumes | Another app is using the GPU. Close it, or raise the thresholds or turn off pausing in Settings → Resources. |
| Replies are very slow | 4-bit bitsandbytes trades speed for memory. Try a smaller model, or turn thinking off in Settings. |
| Replies are fast in short chats but crawl in long chats or long jobs | GPU memory is running out and Windows is quietly using system RAM, about 10× slower. Lower **Context window** in Settings (20,000 is safe for the default model on a 16 GB GPU) and close GPU-heavy apps. Optional: in **NVIDIA Control Panel → Manage 3D settings → CUDA – Sysmem Fallback Policy**, choose **Prefer No Sysmem Fallback**. Then running out of memory shows an error instead of silently slowing down. |
| Port 8765 already in use | `python -m localagent --port 8780` |
| Page loads but nothing updates | Refresh. The page reconnects automatically if the server restarted. |
