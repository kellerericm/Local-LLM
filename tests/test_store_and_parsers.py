from localagent.backend.toolcall_parsers import parse, parse_hermes
from localagent.coordinator.context import estimate_tokens, fit_messages
from localagent.store import Store


def test_store_roundtrip_persists_across_reopen(settings, workspace):
    s = Store(settings.db_path)
    p = s.create_project("Demo", str(workspace), toolsets=["files"])
    general = s.create_chat(None, "general chat")
    chat = s.create_chat(p["id"], "project chat")
    s.add_message(chat["id"], "user", "hi")
    s.add_message(chat["id"], "assistant", "", tool_calls=[{"id": "c1", "name": "list_dir", "arguments": {}}])
    s.add_message(chat["id"], "tool", "ok", tool_call_id="c1", name="list_dir", ok=True)
    s.set_tasks(chat["id"], [{"content": "a", "status": "pending"}])
    s.add_rules(p["id"], ["cmd:network"])
    s.close()

    s2 = Store(settings.db_path)
    assert s2.get_project(p["id"])["toolsets"] == ["files"]
    msgs = s2.list_messages(chat["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1]["tool_calls"][0]["name"] == "list_dir"
    assert msgs[2]["ok"] is True
    assert s2.get_tasks(chat["id"]) == [{"content": "a", "status": "pending"}]
    assert s2.has_rule(p["id"], "cmd:network") and not s2.has_rule("general", "cmd:network")
    assert {c["id"] for c in s2.list_chats()} == {general["id"], chat["id"]}

    s2.delete_project(p["id"])           # cascades to its chats and messages
    assert s2.get_chat(chat["id"]) is None
    assert s2.list_messages(chat["id"]) == []
    assert s2.get_chat(general["id"]) is not None
    s2.close()


def test_pending_approvals_expire_on_restart(settings):
    s = Store(settings.db_path)
    a = s.create_approval("c", "general", ["k"], "sum", "detail")
    s.close()
    s2 = Store(settings.db_path)
    assert s2.get_approval(a["id"])["status"] == "expired"
    s2.close()


def test_parse_plain_answer():
    p = parse_hermes("<think>hmm</think>\n\nThe answer is 4.")
    assert p.reasoning == "hmm" and p.content == "The answer is 4." and not p.tool_calls and not p.errors


def test_parse_tool_calls():
    text = ('Let me look.\n<tool_call>\n{"name": "list_dir", "arguments": {"path": "."}}\n</tool_call>\n'
            '<tool_call>{"name": "read_file", "arguments": "{\\"path\\": \\"a.txt\\"}"}</tool_call>')
    p = parse_hermes(text)
    assert p.content == "Let me look."
    assert p.tool_calls == [{"name": "list_dir", "arguments": {"path": "."}},
                            {"name": "read_file", "arguments": {"path": "a.txt"}}]
    assert not p.errors


def test_parse_repairs_trailing_comma_and_reports_garbage():
    p = parse_hermes('<tool_call>{"name": "x", "arguments": {"a": 1,},}</tool_call>')
    assert p.tool_calls == [{"name": "x", "arguments": {"a": 1}}]
    bad = parse_hermes("<tool_call>{name: x}</tool_call>")
    assert not bad.tool_calls and bad.errors


def test_parse_repairs_unescaped_windows_paths():
    raw = r'<tool_call>{"name": "write_file", "arguments": {"path": "D:\LocalAgent\bench-runs\new\hello.txt", "content": "a\nb"}}</tool_call>'
    p = parse_hermes(raw)
    assert not p.errors
    assert p.tool_calls[0]["arguments"] == {"path": "D:\\LocalAgent\\bench-runs\\new\\hello.txt", "content": "a\nb"}


def test_parse_unclosed_tool_call_and_unclosed_think():
    p = parse_hermes('<tool_call>{"name": "x", "arguments": {}}')
    assert p.tool_calls and any("not closed" in e for e in p.errors)
    t = parse_hermes("<think>still thinking when tokens ran out")
    assert t.content == "" and t.reasoning.startswith("still")


def test_parse_qwen3_coder_xml_with_schema_types():
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {
        "path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}}}},
             {"type": "function", "function": {"name": "update_tasks", "parameters": {"type": "object", "properties": {
                 "tasks": {"type": "array"}}}}}]
    text = ("<think>plan</think>Reading it.\n<tool_call>\n<function=read_file>\n<parameter=path>\n123.txt\n</parameter>\n"
            "<parameter=offset>\n5\n</parameter>\n</function>\n</tool_call>\n<tool_call>\n<function=update_tasks>\n"
            '<parameter=tasks>\n[{"content": "a", "status": "pending"}]\n</parameter>\n</function>\n</tool_call>')
    p = parse(text, tools)
    assert p.format == "qwen3_coder" and p.reasoning == "plan" and p.content == "Reading it."
    assert p.tool_calls[0] == {"name": "read_file", "arguments": {"path": "123.txt", "offset": 5}}
    assert p.tool_calls[1]["arguments"]["tasks"][0]["status"] == "pending"
    assert not p.errors


def test_parse_auto_still_handles_hermes():
    p = parse('<tool_call>{"name": "list_dir", "arguments": {}}</tool_call>')
    assert p.format == "hermes" and p.tool_calls == [{"name": "list_dir", "arguments": {}}]


def test_fit_messages_keeps_system_first_user_and_recent():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for i in range(60):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": {}}}]})
        msgs.append({"role": "tool", "content": "x" * 5000, "tool_call_id": f"c{i}"})
    fitted = fit_messages(msgs, budget_tokens=8000)
    assert fitted[0]["content"] == "sys" and fitted[1]["content"] == "task"
    assert "earlier messages were removed" in fitted[2]["content"]
    assert fitted[-1]["tool_call_id"] == "c59"
    assert sum(estimate_tokens(m) for m in fitted) <= 8000
    # tool results never orphaned from their assistant message
    for i, m in enumerate(fitted):
        if m["role"] == "tool":
            assert fitted[i - 1]["role"] in ("assistant", "tool")
