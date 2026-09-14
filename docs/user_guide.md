# LocalAgent user guide

LocalAgent is an AI agent that runs entirely on your computer. You give it tasks in a chat. It works through them with tools (reading and writing files, running commands and Python), shows you each step, and asks before doing anything outside its workspace.

---

## Chats and projects

**What they're for:** keeping work organized and giving the agent a folder to work in.

- **General chats** (sidebar → *Chats*) aren't tied to any folder. The agent uses a scratch folder (`D:\LocalAgent\data\general_workspace`). Use these for questions, quick experiments, or planning a new project.
- **Projects** (sidebar → *Projects*) are a name plus a **workspace folder**. Every chat in a project works inside that folder. Each project has its own chat list.
- Chats and projects are saved automatically and are still there after you close and reopen the app.

**How to use:**
- **New general chat:** *New chat* at the top of the sidebar.
- **New project:** the **＋** next to *Projects*. Give it a name and a folder, which is created if it doesn't exist.
- **New chat in a project:** the **＋** on the project's row.
- **Rename, move, or delete a chat:** the **⋯** at the top right of the chat. Double-clicking the title also renames it.
- **Project settings or delete:** the **⋯** on the project's row. Deleting a project removes its chats from the app but **never deletes files** in the workspace folder.
- **Let the agent create a project:** in a general chat, say something like *"Set up a project for my thesis notes in D:\Thesis"*. The agent proposes it and you approve it. The chat then moves into the new project.

## Working with the agent

**What it does:** it plans multi-step work, uses tools, checks its results, and reports back.

- Replies stream in live. **Reasoning** (when thinking mode is on) is collapsed under each reply.
- Each **tool call** appears as a card showing the tool, its main argument, and ✓ or ✗. Click it to see the full arguments and output.
- The **task list** panel on the right appears when the agent plans multi-step work. It shows what's pending, in progress (◐), done (●), or blocked (!).
- **Stop** interrupts the agent at any point, including in the middle of a reply or a running command.
- The pill at the top right shows what's happening: *Thinking*, *Waiting for model*, *Loading model…*, *Paused: GPU busy*, or how the last turn ended: *Waiting for your reply*, *Needs your help*, *Step limit reached*, *Stopped*, *Error*.

**When things go wrong:** that's expected, and fine. Errors are fed back to the agent so it can try something else. If several attempts in a row fail, it stops and tells you what it tried and what it needs, instead of spinning. It can also ask you questions directly (a yellow *Question for you* box). Just reply in the chat.

## Approvals and safety

**What they're for:** the agent works freely inside its workspace, but you decide about anything beyond that.

The agent **asks first** (a dialog pops up) when it wants to:
- read or write files outside the workspace (reading the Python environment is allowed)
- install or remove packages (`pip install`, `conda install`, `npm install`)
- use the network (downloads, `git clone`, web requests)
- stop other processes, or run code that can't be checked (e.g. `Invoke-Expression`, Python using `subprocess`)
- create a project

For each request you can choose:
- **Allow once:** just this time.
- **Always allow in this project** (or *in general chats*): this kind of action is remembered for that scope. Review or remove these in **Settings → Saved approvals**.
- **Deny:** the agent is told no and is instructed not to work around it.

These are **always blocked**, with no approval possible: registry edits, permanent environment variables, installing programs (winget, msiexec, choco…), changing services, scheduled tasks, firewall, disks or boot settings, execution policy, permissions, and elevation to admin.

> **Honest limits:** the checks are pattern-based. They catch the common and accidental cases, but they are not a security sandbox. Don't point the agent at folders containing things you can't afford to lose, and keep backups of important work.

## Settings

Open **Settings** at the bottom of the sidebar.

**Model**
- *Model:* a Hugging Face model id (e.g. `Qwen/Qwen3.5-9B`, the default) or a local folder. Changing it reloads the model on the next message, and switches the generation settings to that model's recommended preset. The line under the field shows which model family was recognized, with a link to its model card.
- *Quantization:* `4bit` uses the least VRAM, `8bit` is a middle ground, `none` is full precision and needs far more VRAM.
- *Context window:* how much conversation the model sees at once. Bigger remembers more of a long task but uses more GPU memory and runs slower. The hint shows the model's maximum.
- *Tool-call format:* leave it on `auto` unless a model's tool calls aren't recognized.
- *Models folder / Default Python environment:* where downloads are cached, and where `python`/`pip` point for the agent (projects can override the environment).

**Generation (app default).** Every setting here has a short explanation under it in the app.
- *Preset:* the model publisher's recommended bundles. For Qwen3.5:
  - **Thinking — coding & precise work** is the default and best for agent tasks.
  - **Thinking — general** suits reasoning and writing.
  - **Fast — no thinking** is for quick answers.
  Picking a preset fills in the fields below. Editing any field switches to *Custom*.
- *Reasoning (thinking):* the model reasons privately before answering. It's better on multi-step work but slower.
- *Reasoning budget:* the most tokens it may spend thinking per reply (0 = no limit). When it reaches the budget it's steered to wrap up and act, and that reply is marked *reasoning budget reached*.
- *Temperature:* randomness. Lower is focused and repeatable; higher is more varied but makes more mistakes.
- *Max tokens per reply:* includes the thinking. Raise it if replies stop mid-thought.
- *Advanced sampling:*
  - *Top-p / Top-k / Min-p* limit which words are considered.
  - *Presence penalty* and *Repetition penalty* discourage loops and repeated wording.

**Per-chat model settings.** The **⚙** button in a chat's header opens the same controls for just that chat, e.g. a *Fast* chat for quick questions next to a careful *Thinking* chat. It shows *Default* when the chat follows the app settings, and the preset name (highlighted) when it has its own.

**Usage display**
- Under each reply: how many tokens went to thinking vs. the answer, the speed (tokens/second), and the time taken.
- In the chat header, the **context meter** shows how full the context window was on the last reply. It turns amber at 70% and red at 90%. When it's full, older tool output gets shortened and the oldest turns are dropped.

**Agent**
- *Max steps per turn:* how many model replies one message may use before the agent stops and summarizes.
- *Failures in a row before asking for help.*
- *Default tool timeout:* commands running longer than this are stopped. The agent can ask for more time per command.

**Resources.** The agent is meant to stay out of your way:
- *VRAM limit:* the cap on GPU memory for the model.
- *GPU offload:*
  - **Auto** puts whatever doesn't fit under the VRAM limit into system RAM, capped by *System RAM for offloaded layers*. The offloaded part runs many times slower, and the sidebar status shows how much was offloaded.
  - **GPU only** fails to load instead, so you know to pick a smaller model, lower the context window, or use 4-bit.
- *CPU threads.*
- *Unload model after idle:* frees all VRAM after N minutes without use. It reloads automatically on the next message.
- *Pause when other apps use the GPU:* if a game or render starts using the GPU above the thresholds, generation pauses and resumes when they're done.
- *Background hours:* reserved for long-running background tasks (coming in a later version).
- The agent's model process and every command it runs use **below-normal priority**, so your other programs come first.
- *Unload model now* frees VRAM immediately.

The status lines at the bottom of the sidebar show whether the model is loaded and whether generation is paused for the GPU.

## Tips for good results
- Be specific about the outcome you want and how to check it ("…and run the tests to confirm").
- For big jobs, ask it to make a task list first.
- If it's stuck, tell it what you know. Partial hints help a lot.
- Smaller local models are much less capable than frontier cloud models. Break big tasks into steps.

## Where things are stored
| What | Where |
|---|---|
| Chats, projects, approvals | `D:\LocalAgent\data\localagent.sqlite3` |
| Settings | `D:\LocalAgent\data\settings.json` |
| General chat scratch folder | `D:\LocalAgent\data\general_workspace` |
| Downloaded models | `D:\LocalAgent\models` |
| Benchmark results | `D:\LocalAgent\bench-runs` |

## Not in this version yet
Archiving chats (with compression), downloading and managing models from Hugging Face inside the app, MCP servers/plugins per project, a native desktop window, long-running background tasks, and fine-tuning/LoRA specialists. See the plan in `CLAUDE.md`.
