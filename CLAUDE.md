# Goal
	Create a locally-hosted llm agent capable of running an arbitrary task list to the extent of the frontier agents and to the capability of the 	model. 
# Stack
	Python
	Environment link located in project folder as a short cut. Install whatever packages are necessary for the project
# Conventions
	Use D: for hard drive intensive tasks. 	
	Design description and directly associated notes located in this file.
	Failed tests belong in their own document
	Open questions belong in their own documents
	Experiments underway belong in their own documents

# Design
	Transformers library, preferably a model with a <20 gb hard drive footprint
	Planner/coordinator to facilitate the model's interactions with tools and handle errors and timeouts and such. 
	Long-term task supporting structures on top of this. 
	Locally hosted app. 
	User may create multiple chats, whose states persist through app shutdown
	Chats may be deleted or archived
	Archived chats are compressed to save hard drive space (not merely removed from the UI
	Allows user to select project file directory which contains the agent's workspace. 
	Allow user to configure the environment location if different from above. 
	Allow user to select which mcp servers/plugins/toolsets/whatever are surfaced to the chat in a given workspace
	Workspaces/projects contain their own chat lists.
	General, unaffiliated chat list separate from these.
	Model creates the workspace associations from this general chat when given approval
	Model does not work outside of workspace or environment without approval. 
	Model does not make permanent changes to os, or install programs (plugins and libraries in existing environments are fine, still ask for 	approval).
	Model resources should be secondary to other tasks on the computer. 
	It's fine to fail. It's fine to ask for help. Tell this to it, and know it for you. The goal is improvement, not some ungraspable perfection.  
	A human-legible and human-followable ("cookbook") explaining how to install the system, and additional documentation explaining it's features, 			what they're for, and how they're used, as a user guide. 
	Example long-term tasks: searching documents, while taking notes, which are then compiled into reports. Auto-research style script, formula, 			schematic, or similar design improvements and experimentation.
	Long-term objective: fine-tuning, distillation, and LoRA creation systems for specialist models.
	When the list above is settled, modify this section to reflect what's actually been implemented. 
	If the list is partially settled, add the design description below this line. Delete this line and the line above after the list is exhausted. 

## Implemented so far (Milestone 1, 2026-09-13)
	Code: `localagent/` package. Run: `python -m localagent` (`--fake-model` runs without a GPU). Tests: `python -m pytest tests`. Docs: `docs/cookbook.md`, `docs/user_guide.md`.
	Environment: the existing `local-llm` conda env (Python 3.14.7) works: torch 2.14+cu130, transformers 5.17, bitsandbytes 0.50 (4-bit on CUDA verified). No 3.12 rebuild needed. Node 26 (conda-forge `nodejs`, openssl pinned at 3.5.8) is in the env for development only: `node --check localagent/web/app.js`.
	Data on D:. Models in D:\LocalAgent\models, chats/settings in D:\LocalAgent\data (env var LOCALAGENT_DATA_DIR), benchmark runs in D:\LocalAgent\bench-runs.
	Architecture:
		Web UI: vanilla JS, no build step (`localagent/web`). Talks to a FastAPI server over REST and a WebSocket event stream (`localagent/server`). The API-first design leaves room for a native PySide6 shell later.
		Model: runs in a separate worker process (`backend/worker.py`), so unloading frees all VRAM, it runs at below-normal priority, and crashes are isolated. Backends are swappable behind the `ModelBackend` protocol (`backend/base.py`). `TransformersBackend` handles causal and multimodal checkpoints, bnb 4/8-bit, streaming, per-token pause/cancel, and a per-request LoRA `adapter` hook for future specialists.
		Tool-call parsing (`backend/toolcall_parsers.py`): "hermes" JSON format (Qwen3) and "qwen3_coder" XML format (Qwen3.5+), auto-detected. Repairs common small-model JSON slips (unescaped Windows paths, trailing commas).
		Coordinator (`coordinator/loop.py`): generate → parse → validate args (JSON schema) → safety gate → execute with timeout → feed the result back.
			Malformed or failed calls become feedback to the model. N failures in a row, or the step limit, trigger a tool-less wrap-up where the agent explains and asks for help.
			ask_user ends the turn. Context fitting (`coordinator/context.py`) elides old tool output, then drops the oldest turns.
			The system prompt includes "It's fine to fail. It's fine to ask for help."
		Tools (`tools/`): files (read/write/edit/list/glob/grep), shell (PowerShell), python, core (update_tasks, ask_user; always on), projects (list/create; general chats only). Projects choose which toolsets are enabled.
		Safety (`safety/`), best-effort and not a sandbox:
			PathGuard: realpath and case-normalized. Workspace is read/write, environment is read-only, anything else asks.
			CommandPolicy: deny-list for permanent OS changes and program installers. Asks for package installs, network, process control, dynamic code, and paths outside the workspace.
			ApprovalBroker: once / always-for-this-project / deny, persisted rules.
		Storage (`store/db.py`): SQLite with projects, chats (project_id NULL = general list), messages, per-chat task lists, approvals, and approval rules. Chats persist across restarts. Deleting a project removes its chats, never its files.
		Resources (`resources/manager.py`): worker and child processes at below-normal priority; NVML-based pause when other processes load the GPU (with hysteresis); idle unload; user caps (VRAM, threads, background hours).
	Benchmark: `python -m bench.run --model <id>` runs 10 agentic tasks (`bench/tasks.py`) with automatic checks. Default model chosen from it: Qwen/Qwen3.5-9B (10/10, fastest); the other candidates were deleted. Compare against different model families (older Llama instruct, small Gemma), not other sizes of the same family. See experiments.md.
	Not yet implemented (next phases):
		Phase 2: archive with zstd compression, MCP servers per project, environment-location UI polish, PySide6 native shell.
			Hugging Face model manager (Settings → Models):
				search/browse models and show downloaded ones with disk use
				fit check before download (disk space, estimated VRAM at chosen quantization, tool-calling support, license/gated)
				download with progress, pause/resume, and cancel into the models folder; delete models
				switch the active model, applying that model's recommended presets
				optional HF token for gated models
				option to save a pre-quantized copy
		Phase 3: long-term tasks ("jobs"). Design draft for review: docs/design/phase3_long_running_tasks.md.
			The coordinator owns the structure (persistent job, plan tree, checkpoints, budgets, scheduler); the model does bounded steps in fresh task-scoped contexts.
			Memory lives outside the model: notes with verbatim quotes + SQLite FTS5 search, journal, artifacts, markdown mirrors in <workspace>/jobs/.
			"Done" is decided by machine checks, then a fresh-context reviewer, then user gates.
			Templates: research_report, auto_research, generic. Build order 3a–3d.
			Test workloads in sandbox/ (lake_veyra corpus with answer key; tune_me optimization toy).
		Phase 4: specialist models. Dataset building from transcripts/notes, LoRA/QLoRA with peft, distillation, eval gate on bench/, adapter registry with hot-swap.
		Phase 5: longer-term memory research (open_questions.md).