import json

import pytest
from langchain_core.messages import AIMessage

from reportagent.agent import build_agent
from reportagent.models import PHASE_SETTINGS
from tests.fake_model import ScriptedModel, call


def invoke_tool(tmp_path, phase, **args):
    model = ScriptedModel(responses=[call("calculate_score", **args), AIMessage(content="Done.")])
    result = build_agent(tmp_path, model, phase=phase).invoke({"messages": [("user", "Calculate the scenarios.")]})
    message, = [item for item in result["messages"] if item.type == "tool"]
    return message


@pytest.mark.parametrize("phase", ["review", "revise", "clarify"])
def test_calculator_available_through_phase(tmp_path, phase):
    message = invoke_tool(tmp_path, phase, scores={"water": 30, "A": 55, "B": "68.25"},
                          weights={"water": 0.12, "A": "0.80", "B": "0.08"}, scenarios=[{"water": 88}, {"water": 100}])
    result = json.loads(message.content)
    assert result["base"] == {"F": "53.06", "grade": "III"}
    assert result["scenarios"] == [{"F": "60.02", "grade": "IV"}, {"F": "61.46", "grade": "IV"}]


def test_decimal_boundaries_and_configured_thresholds(monkeypatch):
    from reportagent.agent import calculate_score
    points = ["30", "30.001", "45", "45.005", "60", "60.001"]
    args = dict(scores={"x": 0}, weights={"x": 1}, scenarios=[{"x": p} for p in points])
    assert calculate_score.invoke(args)["scenarios"] == [
        {"F": "30.00", "grade": "I"}, {"F": "30.00", "grade": "II"}, {"F": "45.00", "grade": "II"},
        {"F": "45.01", "grade": "III"}, {"F": "60.00", "grade": "III"}, {"F": "60.00", "grade": "IV"}]
    monkeypatch.setitem(PHASE_SETTINGS["grading"], "upper_bounds", [25, 40, 65])
    assert calculate_score.invoke(args)["scenarios"][-1]["grade"] == "III"


@pytest.mark.parametrize("scores,weights,scenarios", [
    ({}, {}, []), ({"a": 40}, {"b": 1}, []), ({"a": 40}, {"a": 1}, [{"b": 70}]),
])
def test_calculator_reports_bad_inputs(tmp_path, scores, weights, scenarios):
    msg = invoke_tool(tmp_path, "revise", scores=scores, weights=weights, scenarios=scenarios)
    assert set(json.loads(msg.content)) == {"error"}


def test_calculator_is_not_exposed_in_source_verification(tmp_path):
    message = invoke_tool(tmp_path, "verify-sources", scores={"x": 60}, weights={"x": 1})
    assert message.status == "error"
