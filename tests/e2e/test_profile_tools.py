import json
import math
from pathlib import Path
from shutil import copyfile

import pytest
from langchain_core.messages import AIMessage

from reportagent.agent import build_agent
from tests.fake_model import ScriptedModel, call


@pytest.fixture
def drawing_case(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    for name in ("profile.pdf", "profile-without-ticks.pdf"):
        copyfile(Path(__file__).parents[1] / "fixtures" / name, sources / name)
    return tmp_path


def tool_result(case, phase, name, **args):
    model = ScriptedModel(responses=[call(name, **args), AIMessage(content="Done.")])
    graph = build_agent(case, model, phase=phase, section="04-4")
    result = graph.invoke({"messages": [("user", "Inspect the drawing.")]})
    return next(message for message in result["messages"] if message.type == "tool")


@pytest.mark.parametrize("phase", ["gather", "rescore"])
def test_measure_known_profile(drawing_case, phase):
    location = dict(file="/sources/profile.pdf", page=1)
    layers = json.loads(tool_result(drawing_case, phase, "list_profile_layers", **location).content)
    assert layers["layers"]["dmx"]["path_count"] == 1
    assert layers["layers"]["dmx"]["x_extent_m"] == pytest.approx([0, 40])
    assert layers["layers"]["dmx"]["y_extent_m"] == pytest.approx([20, 40])
    for axis in ("x_calibration", "y_calibration"):
        assert len(layers[axis]["ticks"]) == 5
        assert layers[axis]["max_residual_m"] == pytest.approx(0)
    result = json.loads(tool_result(drawing_case, phase, "measure_profile", **location,
                                    x_from=0, x_to=40).content)
    assert result["H0_m"] == pytest.approx(20)
    assert result["L0_m"] == pytest.approx(40)
    assert result["alpha0_deg"] == pytest.approx(math.degrees(math.atan(0.5)), abs=0.1)


@pytest.mark.parametrize("name,page,layer", [
    ("profile-without-ticks.pdf", 1, "dmx"),
    ("profile.pdf", 1, "absent"),
    ("profile.pdf", 2, "dmx"),
    ("../work/private.pdf", 1, "dmx"),
])
def test_unavailable_measurements_return_data(drawing_case, name, page, layer):
    if name.startswith(".."):
        (drawing_case / "work").mkdir(exist_ok=True)
        copyfile(drawing_case / "sources/profile.pdf", drawing_case / "work/private.pdf")
    result = tool_result(drawing_case, "rescore", "measure_profile", file="/sources/" + name,
                         page=page, layer=layer, x_from=0, x_to=40)
    assert result.status == "success"
    assert json.loads(result.content)["unavailable"]


@pytest.mark.parametrize("phase", ["review", "sync", None])
def test_measurement_is_limited_to_gather_and_rescore(drawing_case, phase):
    result = tool_result(drawing_case, phase, "list_profile_layers", file="/sources/profile.pdf", page=1)
    assert result.status == "error"


@pytest.mark.parametrize("phase", ["gather", "rescore"])
def test_cli_offers_both_measurement_tools(drawing_case, monkeypatch, phase):
    from langchain_core.messages import HumanMessage
    from reportagent import cli

    offered = []
    original = ScriptedModel.bind_tools

    def record(self, tools, **kwargs):
        offered.append({tool.name for tool in tools})
        return original(self, tools, **kwargs)

    monkeypatch.setattr(ScriptedModel, "bind_tools", record)
    args = dict(file="/sources/profile.pdf", page=1, x_from=40, x_to=0)
    model = ScriptedModel(responses=[call("measure_profile", **args), AIMessage(content="Done.")])
    monkeypatch.setattr(cli, "make_model", lambda phase: model)
    log, _ = cli._run_phase(drawing_case, phase, [HumanMessage(content="Measure the profile.")],
                            100000, drawing_case, section="04-4")
    assert {"list_profile_layers", "measure_profile"} <= offered[0]
    result = next(item for item in map(json.loads, log.read_text().splitlines()) if item["type"] == "tool")
    assert json.loads(result["content"])["alpha0_deg"] == pytest.approx(-26.565, abs=0.1)


@pytest.mark.parametrize("start,end", [(10, 10), (-5, 5)])
def test_unmeasurable_interval_returns_no_angle(drawing_case, start, end):
    result = tool_result(drawing_case, "rescore", "measure_profile", file="/sources/profile.pdf",
                         page=1, x_from=start, x_to=end)
    data = json.loads(result.content)
    assert "unavailable" in data and "alpha0_deg" not in data


def test_unscaled_layers_stay_visible(drawing_case):
    result = tool_result(drawing_case, "gather", "list_profile_layers", file="/sources/profile-without-ticks.pdf", page=1)
    data = json.loads(result.content)
    assert data["unfitted_labels"] == {"x": [], "y": []}
    assert data["layers"]["dmx"] == {"path_count": 1, "x_extent_m": None, "y_extent_m": None}


def test_rescore_compares_the_measurement_with_the_drawings():
    from importlib.resources import files

    lines = files("reportagent.skills").joinpath("rescore-section", "SKILL.md").read_text(encoding="utf-8").splitlines()
    x12 = next(i for i, line in enumerate(lines) if line.startswith("For X12, measure the natural ground angle"))
    assert lines[x12 + 1] == (
        "If the drawings or design notes state this slope's natural slope angle as a range, use its lower bound as α₀ "
        "so that the risk is not underestimated, cite it, and report the measure_profile result beside it as a check; "
        "if they state only a maximum or no angle, use the measured α₀ and cite the stated maximum beside it."
    )
