"""Freeze one historical phase and measure its recorded outcome without calling a model."""
import argparse
from hashlib import sha256
from itertools import takewhile
import json
from pathlib import Path
from shutil import copy2, copytree
from tempfile import TemporaryDirectory

from deepagents.backends import FilesystemBackend

from reportagent.agent import PHASE_SKILLS
from reportagent.cli import _fixture_digest, _replay_metrics
from reportagent.learn import file_operations, recorded_turns


def freeze(pack, phase, source, state, transcript, criteria):
    pack.mkdir(parents=True, exist_ok=True)
    target = pack / phase
    if target.exists():
        raise SystemExit(f"Frozen phase already exists: {target}")
    lines = transcript.read_bytes().splitlines(keepends=True)
    initial = list(takewhile(lambda line: json.loads(line)["type"] == "human", lines))
    messages = [json.loads(line) for line in lines]
    if not initial or not any(m["type"] == "ai" for m in messages):
        raise SystemExit("Expected initial human messages and a recorded model response.")
    with TemporaryDirectory(dir=pack, prefix=".freeze-") as staging:
        tmp = Path(staging)
        common = tmp / "shared"
        common.mkdir()
        for name in ("sources", "reference", "commission", "index.md"):
            path = source / name
            if path.exists():
                (copytree if path.is_dir() else copy2)(path, common / path.name)
        shared_hash = _fixture_digest(common)
        shared = pack / "shared"
        if shared.exists() and _fixture_digest(shared) != shared_hash:
            raise SystemExit("The phase used different shared case inputs.")
        fixture = tmp / phase
        fixture.mkdir()
        def exclude_baselines(directory, names):
            return {"replay", "golden"} if Path(directory) == state / "work" else set()
        copytree(state / "work", fixture / "state/work", ignore=exclude_baselines)
        if (state / "out").is_dir():
            copytree(state / "out", fixture / "state/out")
        (fixture / "inputs.jsonl").write_bytes(b"".join(initial))
        copy2(criteria, fixture / "criteria.json")
        work = tmp / "outcome"
        copytree(fixture / "state", work)
        copytree(common, work, dirs_exist_ok=True)
        name = pack.name
        before = (work / f"work/sections/{name}.md").read_text(encoding="utf-8").splitlines()
        backend = FilesystemBackend(root_dir=work, virtual_mode=True)
        for _, call, success in file_operations(recorded_turns(messages)):
            if success:
                op = backend.edit if call["name"] == "edit_file" else backend.write
                result = op(**call["args"])
                if result.error:
                    raise SystemExit(f"Historical operation did not reproduce: {result.error}")
        scores, details = _replay_metrics(work, name, phase, json.loads(criteria.read_text(encoding="utf-8")), before, messages)
        (fixture / "baseline.json").write_text(json.dumps(scores, indent=2) + "\n", encoding="utf-8")
        manifest = dict(phase=phase, transcript=transcript.name, transcript_sha256=sha256(transcript.read_bytes()).hexdigest(),
                        initial_messages=len(initial), shared_sha256=shared_hash, state_sha256=_fixture_digest(fixture / "state"),
                        historical_result=details, files={p.name: sha256(p.read_bytes()).hexdigest() for p in fixture.iterdir() if p.is_file()})
        saved = work / details["target"]
        if scores["target_saved"] and saved.is_file():
            manifest["historical_target_sha256"] = sha256(saved.read_bytes()).hexdigest()
        (fixture / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not shared.exists():
            common.rename(shared)
        fixture.rename(target)
    print(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", type=Path, help="work/replay/<exact section stem>")
    parser.add_argument("phase", choices=PHASE_SKILLS)
    parser.add_argument("source", type=Path, help="historical case, for shared inputs")
    parser.add_argument("state", type=Path, help="directory containing the pre-phase work/ and out/")
    parser.add_argument("transcript", type=Path)
    parser.add_argument("criteria", type=Path, help="expected_ids for verify or finding_lines for revise")
    freeze(**vars(parser.parse_args()))
