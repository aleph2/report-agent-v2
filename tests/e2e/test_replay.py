import json
import sys
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from reportagent import cli
from tests.fake_model import ScriptedModel, call
from scripts.freeze_replay import freeze


@pytest.mark.parametrize("historical", [False, True])
def test_skill_replay_runs_in_isolation_on_frozen_inputs(tmp_path, monkeypatch, historical):
    case = tmp_path / "original"
    pack = case / "work/replay/demo"
    (pack / "shared").mkdir(parents=True)
    phase = pack / "revise"
    for folder in ("sections", "clarifications"):
        (phase / "state/work" / folder).mkdir(parents=True)
    section = phase / "state/work/sections/demo.md"
    section.write_text("first\ncited\nlast")
    (phase / "state/work/clarifications/demo.md").write_text("number: 2\nnumber: 2")
    (phase / "inputs.jsonl").write_text(HumanMessage(content="Exact frozen input").model_dump_json() + "\n")
    criteria = {"finding_lines": {"A": [2], "B": [3]}, "expected_ids": []}
    (phase / "criteria.json").write_text(json.dumps(criteria))
    target = "/work/sections/demo.md"
    edits = [dict(name="edit_file", id=f"e{i}", args=dict(file_path=target, old_string=a, new_string=b))
             for i, (a, b) in enumerate([("cited", "changed"), ("absent", "never")])]
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=edits, usage_metadata={
        "input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "output_token_details": {"reasoning": 9}}),
        AIMessage(content="Done", usage_metadata={"input_tokens": 20, "output_tokens": 1, "total_tokens": 21, "output_token_details": {"reasoning": 0}})])
    baseline = dict(edits_submitted=2, edits_applied=0, edits_rejected=0, first_response_saved=1,
                    target_saved=1, must_fix_touched=0, duplicate_numbers=1, f_grade_agreements=0,
                    f_grade_differences=0, total_tokens=141, reasoning_tokens=9)
    (phase / "baseline.json").write_text(json.dumps(baseline))
    manifest = dict(shared_sha256=cli._fixture_digest(pack / "shared"), state_sha256=cli._fixture_digest(phase / "state"),
                    files={f: sha256((phase / f).read_bytes()).hexdigest() for f in ("inputs.jsonl", "baseline.json", "criteria.json")})
    (phase / "manifest.json").write_text(json.dumps(manifest))
    def model_for(phase):
        assert phase == "revise"
        return model
    monkeypatch.setattr(cli, "make_model", model_for)
    monkeypatch.setattr(sys, "argv", ["reportagent", "replay", str(case), "--skill", "write-section"]
                        + (["--historical-input"] if historical else []))
    cli.main()
    run, = tmp_path.glob("reportagent-replay-*")
    result, = json.loads((run / "results.json").read_text())
    m = result["metrics"]
    assert m["must_fix_touched"]["actual"] == 1 and m["edits_applied"]["change"] == "better"
    assert m["edits_rejected"]["change"] == "worse" and m["first_response_saved"]["change"] == "same"
    assert m["reasoning_tokens"]["actual"] == 9 and m["total_tokens"]["actual"] == 141
    assert m["duplicate_numbers"]["actual"] == 1 and section.read_text() == "first\ncited\nlast"
    log, = (run / "demo").glob("*-revise.jsonl")
    content = json.loads(log.read_text().splitlines()[0])["content"]
    if historical:
        assert content == "Exact frozen input"
    else:
        assert "revise section demo" in content and "2\tcited" in content and "Exact frozen input" not in content
    assert result["input_mode"] == ("historical" if historical else "current")
    assert m["must_fix_touched"]["label"] == "edits touching finding lines"
    req, = (run / "demo").glob("*.requests.jsonl")
    assert json.loads(req.read_text().splitlines()[0])["remaining_tokens"] == result["phase_cap"] == 130000
    assert {p.name for p in (case / "work").iterdir()} == {"replay"}


def test_replay_refuses_missing_fixture_before_model(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "make_model", lambda *_: pytest.fail("unexpected model"))
    monkeypatch.setattr(sys, "argv", ["reportagent", "replay", str(tmp_path), "--skill", "verify-sources"])
    with pytest.raises(SystemExit, match="frozen"):
        cli.main()


def test_freeze_and_replay_counts_only_ids_saved_by_this_run(tmp_path, monkeypatch):
    source, state = tmp_path / "source", tmp_path / "state"
    source.mkdir()
    (state / "work/sections").mkdir(parents=True)
    (state / "work/sections/demo.md").write_text("Section")
    target = "/work/reviews/demo-sources.md"
    path = state / target.lstrip("/")
    path.parent.mkdir()
    path.write_text("| 1 | already present |")
    (state / "work/checks").mkdir()
    (state / "work/checks/demo.md").write_text("Current frozen checklist\n## Findings\nOld diagnostics")
    prompt = [HumanMessage(content=[{"type": "text", "text": "checklist"}]), HumanMessage(content="Save demo")]
    reply = call("write_file", file_path=target, content="| 1 | old |\n| 1 | old |\n| 3 | extra |")
    rows = [*prompt, reply, ToolMessage(content="Updated file " + target, tool_call_id="write_file")]
    transcript, criteria = tmp_path / "trace.jsonl", tmp_path / "criteria.json"
    transcript.write_text("".join(m.model_dump_json() + "\n" for m in rows))
    criteria.write_text('{"expected_ids":[1,2]}')
    pack = source / "work/replay/demo"
    freeze(pack, "verify-sources", source, state, transcript, criteria)
    fixture = pack / "verify-sources"
    assert [json.loads(l)["content"] for l in (fixture / "inputs.jsonl").read_text().splitlines()] == [m.content for m in prompt]
    manifest = json.loads((fixture / "manifest.json").read_text())
    assert manifest["historical_result"]["extra_ids"] == [3]
    model = ScriptedModel(responses=[AIMessage(content="No file written.", usage_metadata={
        "input_tokens": 20, "output_tokens": 1, "total_tokens": 21})])
    monkeypatch.setattr(cli, "make_model", lambda phase: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "replay", str(source), "--skill", "verify-sources"])
    cli.main()
    run, = tmp_path.glob("reportagent-replay-*")
    result, = json.loads((run / "results.json").read_text())
    assert result["metrics"]["source_ids_saved"] == dict(baseline=1, actual=0, change="worse")
    log, = (run / "demo").glob("*-verify-sources.jsonl")
    rebuilt = [json.loads(line)["content"] for line in log.read_text().splitlines()[:2]]
    assert rebuilt[0] == "Current frozen checklist" and "Save the source checks" in rebuilt[1]
    assert result["phase_cap"] == 90000
    assert result["missing_ids"] == [1, 2] and result["execution_status"] == "completed"
    (fixture / "inputs.jsonl").write_text(HumanMessage(content="altered").model_dump_json())
    with pytest.raises(SystemExit, match="changed"):
        cli.main()


@pytest.mark.parametrize("batch,limits", [
    ("verify-review", [("verify-sources", 90000), ("review", 100000)]),
    ("clarify-revise", [("clarify", 100000), ("revise", 130000)]),
])
def test_replay_batch_selects_one_section(tmp_path, monkeypatch, batch, limits):
    case, state = tmp_path / "case", tmp_path / "state"
    pack = case / "work/replay/demo"
    (pack.parent / "other").mkdir(parents=True)
    for folder in ("sections", "checks"):
        path = state / "work" / folder / "demo.md"
        path.parent.mkdir(parents=True)
        path.write_text("Demo")
    trace, criteria = tmp_path / "trace.jsonl", tmp_path / "criteria.json"
    reply = AIMessage(content="Complete", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    trace.write_text(HumanMessage(content="Historical").model_dump_json() + "\n" + reply.model_dump_json() + "\n")
    criteria.write_text('{"expected_ids": [], "finding_lines": {}}')
    for phase, _ in limits:
        freeze(pack, phase, case, state, trace, criteria)
    argv = ["reportagent", "replay", str(case), "--batch", batch]
    monkeypatch.setattr(cli, "make_model", lambda _: pytest.fail("no section selected"))
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit):
        cli.main()
    monkeypatch.setattr(cli, "make_model", lambda _: ScriptedModel(responses=[reply]))
    monkeypatch.setattr(sys, "argv", [*argv, "--section", "demo"])
    cli.main()
    run, = tmp_path.glob("reportagent-replay-*")
    result = json.loads((run / "results.json").read_text())
    assert [(row["section"], row["phase"], row["phase_cap"]) for row in result] == [
        ("demo", phase, limit) for phase, limit in limits]
    for phase, limit in limits:
        log, = (run / "demo").glob(f"*-{phase}.requests.jsonl")
        assert json.loads(log.read_text().splitlines()[0])["remaining_tokens"] == limit
