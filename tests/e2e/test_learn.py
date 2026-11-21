from pathlib import Path
import json
import sys

from reportagent import cli, learn


def test_signal_labels_and_unanswered_requests(tmp_path):
    log = tmp_path / "20260101T000000Z-review.jsonl"
    target = "/work/reviews/demo.md"
    first = {"id": "r", "name": "read_file", "args": {"file_path": "/work/plan.md"}}
    save = {"id": "w", "name": "write_file", "args": {"file_path": target}}
    records = []
    attempts = []
    for i, calls, result, status in [(1, [first], "This phase may read /sources/ pages.", "error"),
                                    (3, [first], "Error: File missing", "error"),
                                    (4, [save], "Updated file " + target, "success")]:
        records.extend([{"type": "ai", "tool_calls": calls, "content": ""},
                        {"type": "tool", "tool_call_id": calls[0]["id"], "content": result, "status": status}])
        attempts.append({"request": i, "event": "response", "input_tokens": 8, "output_tokens": 2, "total_tokens": 10})
    attempts.extend([{"request": i, "decision": "send", "estimated_input_tokens": 7} for i in (1, 2, 3, 4)])
    attempts.append({"request": 5, "decision": "stop"})
    log.write_text("".join(json.dumps(x) + "\n" for x in records))
    log.with_suffix(".requests.jsonl").write_text("".join(json.dumps(x) + "\n" for x in attempts))
    phase = learn._phase_signal(log, "review", "demo", 100)
    assert [r.get("outcome") for r in phase["requests"]] == ["denied", None, "other", "saved", None]
    assert phase["requests"][2]["tool_results"][0]["content"] == "Error: File missing"
    assert phase["requests"][0]["denied_paths"] == ["/work/plan.md"]
    assert phase["wasted_tokens"] == phase["other_tokens"] == 10
    assert phase["unanswered"] == phase["guard_stops"] == 1
    assert phase["reserve_before_save"] is False


def test_learning_rebuild_and_summary(tmp_path, monkeypatch, capsys):
    runs = tmp_path / "work/runs"
    runs.mkdir(parents=True)
    (runs / "20260101T000000Z-review.jsonl").write_text('{"type":"ai","content":"Done."}\n')
    def invoke(*args):
        monkeypatch.setattr(sys, "argv", ["reportagent", "learn", *args, "--case", str(tmp_path)])
        cli.main()
    invoke("signals")
    file, = (tmp_path / "work/learn/signals").glob("*.json")
    first = file.read_bytes()
    invoke("signals")
    assert first == file.read_bytes()
    record = json.loads(first)
    assert record["header"]["partial"] and record["header"]["task"] == "unknown"
    invoke("summary")
    assert "other_tokens" in capsys.readouterr().out


def test_run_signals_keep_feedback_and_distinguish_touched_from_resolved(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage
    from test_run import ScriptedModel

    work = tmp_path / "work"
    for folder, body in {"sections": "original", "notes": "notes", "clarifications": "number: 8",
                         "reviews": "F1｜method｜line 1｜must-fix"}.items():
        path = work / folder / "demo.md"
        path.parent.mkdir(parents=True)
        path.write_text(body)
    # A scripted revision edits the cited line. No later reviewer has accepted it yet.
    call = {"id": "edit1", "name": "edit_file",
            "args": {"file_path": "/work/sections/demo.md", "old_string": "original", "new_string": "revised"}}
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[call]), AIMessage(content="Done")])
    monkeypatch.setattr(cli, "make_model", lambda phase: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "revise", str(tmp_path), "--phases", "revise",
                                     "--section", "demo", "--max-tokens", "100000", "--task", "9"])
    cli.main()
    signal, = (work / "learn/signals").glob("*.json")
    record = json.loads(signal.read_text())
    assert record["header"]["task"] == 9 and record["S5"]["F1"]["status"] == "touched"
    assert record["S4"][0]["before"]["lines"] and record["S7"][0]["numbers_after"] == [8]
    event = dict(run_id=signal.stem, section="demo", indicator="X11", agent_value=40, evidence="design p.3",
                 band="25–49", agent_reason="documented height", owner_value=45, owner_reason="revised survey",
                 date="2026-01-03", source="owner email")
    file = tmp_path / "event.json"
    for value in (45, 45, 48):
        event["owner_value"] = value
        file.write_text(json.dumps(event))
        monkeypatch.setattr(sys, "argv", ["reportagent", "learn", "feedback", "--input", str(file), "--case", str(tmp_path)])
        cli.main()
    record = json.loads(signal.read_text())
    assert [e["owner_value"] for e in record["S6"]] == [45, 48]
    assert all(e["agent_value"] == 40 and e["evidence"] == "design p.3" for e in record["S6"])
    assert (work / "sections/demo.md").read_text() == "revised"
    # A later saved review on this section explicitly lists another finding.
    review = {"id": "w2", "name": "write_file",
              "args": {"file_path": "/work/reviews/demo.md", "content": "F2｜method｜line 1｜must-fix"}}
    monkeypatch.setattr(cli, "make_model", lambda phase: ScriptedModel(
        responses=[AIMessage(content="", tool_calls=[review]), AIMessage(content="Done")]))
    monkeypatch.setattr(sys, "argv", ["reportagent", "revise", str(tmp_path), "--phases", "review",
                                      "--section", "demo", "--max-tokens", "100000"])
    cli.main()
    assert json.loads(signal.read_text())["S5"]["F1"]["status"] == "resolved-by-review"
    owner = dict(run_id=signal.stem, section="demo", finding_id="F1", date="2026-01-04",
                 source="owner email", owner_reason="original page confirmed")
    file.write_text(json.dumps(owner))
    monkeypatch.setattr(sys, "argv", ["reportagent", "learn", "feedback", "--case", str(tmp_path), "--input", str(file)])
    cli.main()
    assert json.loads(signal.read_text())["S5"]["F1"]["status"] == "resolved-by-owner"
