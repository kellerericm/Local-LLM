import json
import threading

from localagent.backend.fake import ScriptedBackend
from localagent.coordinator import Coordinator
from localagent.safety import ApprovalBroker, AutoApprover
from localagent.tools import default_registry


def call(tool_name, **args):
    return f'<tool_call>{json.dumps({"name": tool_name, "arguments": args})}</tool_call>'


class Events(list):
    def __call__(self, ev):
        self.append(ev)

    def of(self, t):
        return [e for e in self if e["type"] == t]


def make(store, settings, responses, approver=None):
    events = Events()
    backend = ScriptedBackend(responses)
    coord = Coordinator(backend, store, default_registry(), approver or AutoApprover(allow=False),
                        lambda: settings, events)
    return coord, backend, events


def project_chat(store, workspace):
    p = store.create_project("P", str(workspace))
    return store.create_chat(p["id"])["id"]


def test_multi_step_task_with_tools(store, settings, workspace):
    chat = project_chat(store, workspace)
    coord, backend, events = make(store, settings, [
        call("update_tasks", tasks=[{"content": "write file", "status": "in_progress"}]),
        call("write_file", path="hello.py", content="print('hi')\n"),
        call("run_python", code="import runpy; runpy.run_path('hello.py')"),
        "Done: created hello.py and ran it.",
    ])
    assert coord.run(chat, "make hello.py") == "done"
    assert (workspace / "hello.py").read_text() == "print('hi')\n"
    tool_msgs = [m for m in store.list_messages(chat) if m["role"] == "tool"]
    assert all(m["ok"] for m in tool_msgs), [m["content"] for m in tool_msgs]
    assert "hi" in tool_msgs[-1]["content"]
    assert store.get_tasks(chat)[0]["status"] == "in_progress"
    # the model saw the tool results on its later calls, paired with call ids
    last_msgs = backend.calls[-1]["messages"]
    assert last_msgs[0]["role"] == "system" and str(workspace) in last_msgs[0]["content"]
    assert any(m["role"] == "tool" and "hi" in m["content"] for m in last_msgs)
    assert events.of("status")[-1]["outcome"] == "done"


def test_malformed_call_gets_feedback_then_recovers(store, settings, workspace):
    chat = project_chat(store, workspace)
    coord, backend, _ = make(store, settings, [
        "<tool_call>{not json}</tool_call>",
        call("list_dir"),
        "Listed.",
    ])
    assert coord.run(chat, "list") == "done"
    notes = [m for m in store.list_messages(chat) if m["kind"] == "coordinator"]
    assert notes and "could not be understood" in notes[0]["content"]
    assert "[coordinator]" in backend.calls[1]["messages"][-1]["content"]


def test_invalid_args_and_unknown_tool_are_reported(store, settings, workspace):
    chat = project_chat(store, workspace)
    coord, _, _ = make(store, settings, [call("read_file"), call("teleport", where="moon"), "ok"])
    coord.run(chat, "x")
    results = [m["content"] for m in store.list_messages(chat) if m["role"] == "tool"]
    assert "Invalid arguments for read_file" in results[0] and "'path' is a required property" in results[0]
    assert "Unknown tool 'teleport'" in results[1]


def test_repeated_failures_stop_and_ask_for_help(store, settings, workspace):
    chat = project_chat(store, workspace)
    settings.max_consecutive_failures = 3
    coord, backend, _ = make(store, settings, [
        call("read_file", path="missing1.txt"),
        call("read_file", path="missing2.txt"),
        call("read_file", path="missing3.txt"),
        "I couldn't find the files. Where are they?",
    ])
    assert coord.run(chat, "read stuff") == "needs_help"
    assert backend.calls[-1]["tools"] is None           # wrap-up turn has no tools
    msgs = store.list_messages(chat)
    assert msgs[-1]["role"] == "assistant" and "couldn't find" in msgs[-1]["content"]


def test_denied_outside_path(store, settings, workspace, tmp_path):
    chat = project_chat(store, workspace)
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    approver = AutoApprover(allow=False)
    coord, _, _ = make(store, settings, [call("read_file", path=str(secret)), "ok, I won't."], approver)
    coord.run(chat, "read the secret")
    result = [m for m in store.list_messages(chat) if m["role"] == "tool"][0]
    assert not result["ok"] and "denied" in result["content"] and "top secret" not in result["content"]
    assert approver.requests and approver.requests[0]["scope"] != "general"


def test_calls_after_a_denial_in_same_message_are_skipped(store, settings, workspace, tmp_path):
    chat = project_chat(store, workspace)
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    batch = call("read_file", path=str(secret)) + call("write_file", path="copy.txt", content="invented contents")
    coord, _, _ = make(store, settings, [batch, "ok"], AutoApprover(allow=False))
    coord.run(chat, "copy the secret")
    results = [m for m in store.list_messages(chat) if m["role"] == "tool"]
    assert "denied" in results[0]["content"]
    assert results[1]["content"].startswith("Not run") and not results[1]["ok"]
    assert not (workspace / "copy.txt").exists()


def test_policy_blocks_os_changes_without_asking(store, settings, workspace):
    chat = project_chat(store, workspace)
    approver = AutoApprover(allow=True)
    coord, _, _ = make(store, settings, [call("run_shell", command="setx FOO bar"), "blocked."], approver)
    coord.run(chat, "set env var")
    result = [m for m in store.list_messages(chat) if m["role"] == "tool"][0]
    assert "Blocked by policy" in result["content"] and not approver.requests


def test_shell_timeout(store, settings, workspace):
    chat = project_chat(store, workspace)
    coord, _, _ = make(store, settings, [call("run_shell", command="Start-Sleep -Seconds 30", timeout_s=2), "timed out"])
    coord.run(chat, "sleep")
    result = [m for m in store.list_messages(chat) if m["role"] == "tool"][0]
    assert not result["ok"] and "Timed out after 2s" in result["content"]


def test_ask_user_ends_turn(store, settings, workspace):
    chat = project_chat(store, workspace)
    coord, backend, _ = make(store, settings, [call("ask_user", question="Which format?"), "never reached"])
    assert coord.run(chat, "make a report") == "waiting_user"
    assert len(backend.calls) == 1


def test_step_limit_wraps_up(store, settings, workspace):
    chat = project_chat(store, workspace)
    settings.max_steps = 3
    coord, backend, _ = make(store, settings, [call("list_dir")] * 3 + ["Summary: still listing."])
    assert coord.run(chat, "loop forever") == "step_limit"
    assert "Summary" in store.list_messages(chat)[-1]["content"]


def test_approval_broker_once_always_deny(store, settings, workspace, tmp_path):
    chat = project_chat(store, workspace)
    project_id = store.get_chat(chat)["project_id"]
    events = []
    broker = ApprovalBroker(store, events.append)
    outcomes = []

    def ask():
        outcomes.append(broker.request(chat, project_id, ["cmd:network"], "Run", "curl x"))

    for decision in ("deny", "always"):
        t = threading.Thread(target=ask)
        t.start()
        for _ in range(100):
            if broker.pending():
                break
            threading.Event().wait(0.02)
        broker.resolve(broker.pending()[0]["id"], decision)
        t.join(2)
    assert outcomes == [False, True]
    assert broker.request(chat, project_id, ["cmd:network"], "Run", "curl y") is True   # remembered
    assert not broker.pending()


def test_general_chat_creates_project_and_moves(store, settings, workspace, tmp_path):
    chat = store.create_chat(None)["id"]
    target = tmp_path / "new_project"
    coord, backend, events = make(store, settings, [
        call("create_project", name="Report", workspace_path=str(target)),
        call("write_file", path="notes.md", content="# notes"),
        "Created the project and started notes.",
    ], AutoApprover(allow=True))
    assert coord.run(chat, "start a report project") == "done"
    project = store.list_projects()[0]
    assert store.get_chat(chat)["project_id"] == project["id"]
    assert (target / "notes.md").exists()                 # later steps use the new workspace
    assert "create_project" in [t["function"]["name"] for t in backend.calls[0]["tools"]]
    assert "create_project" not in [t["function"]["name"] for t in backend.calls[1]["tools"]]


def test_cancel_stops_run(store, settings, workspace):
    chat = project_chat(store, workspace)
    cancel = threading.Event()

    def slow(messages):
        cancel.set()
        return "partial answer " * 20

    coord, _, _ = make(store, settings, [slow])
    assert coord.run(chat, "go", cancel) == "cancelled"


def test_repeated_identical_reads_are_withheld_and_end_in_needs_help(store, settings, workspace):
    # Dry run 5: the model alternated search_notes / read_file with slightly different arguments for 30 steps.
    chat = project_chat(store, workspace)
    settings.max_steps = 20
    (workspace / "a.txt").write_text("alpha\nbeta\n")
    coord, backend, _ = make(store, settings, [
        call("read_file", path="a.txt"),
        call("write_file", path="b.txt", content="x"),        # a change: earlier reads no longer count
        call("read_file", path="a.txt", limit=100),
        call("list_dir"),
        call("read_file", path="a.txt", limit=200),           # same output as the read before it: warned
        call("list_dir"),
        call("read_file", path="a.txt"),                      # third time: withheld
        call("list_dir"),
        call("read_file", path="a.txt", limit=50),
        "I kept re-reading; I need help deciding what to write.",
    ])
    assert coord.run(chat, "summarize a.txt") == "needs_help"
    tools = [m for m in store.list_messages(chat) if m["role"] == "tool"]
    assert tools[2]["ok"] and "Same" not in tools[2]["content"] and "exactly the same" not in tools[2]["content"]
    assert tools[4]["ok"] and tools[4]["content"].startswith("(This is exactly the same output")
    assert not tools[6]["ok"] and tools[6]["content"].startswith("Not shown") and "alpha" not in tools[6]["content"]
    assert len(tools) == 9


def test_rereading_a_long_output_after_it_was_shortened_is_allowed(store, settings, workspace):
    # Dry run 8: a 7 KB outline read at step 1 was shortened by context fitting, re-read, and wrongly withheld.
    chat = project_chat(store, workspace)
    settings.max_steps = 20
    (workspace / "big.md").write_text("".join(f"line {i} of the outline with some words\n" for i in range(300)))
    for i in range(5):
        (workspace / f"small{i}.txt").write_text(f"tiny {i}")
    coord, backend, _ = make(store, settings, [
        call("read_file", path="big.md", limit=400),
        *[call("read_file", path=f"small{i}.txt") for i in range(5)],                    # other work in between
        call("list_dir", path="."),
        call("read_file", path="big.md", limit=400),       # long ago now: legitimately re-read
        call("read_file", path="big.md", limit=400),       # immediately again: still visible, so warned
        "done",
    ])
    assert coord.run(chat, "read things") == "done"
    tools = [m for m in store.list_messages(chat) if m["role"] == "tool"]
    big = [m for m in tools if "line 0 of the outline" in m["content"] or "Not shown" in m["content"]]
    assert big[1]["ok"] and not big[1]["content"].startswith("(This is exactly")
    assert big[2]["content"].startswith("(This is exactly the same output")
