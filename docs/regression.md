# Offline merge regression

From the repository root, run this on the proposed merge result, and repeat on the actual merged commit. Keep the output with the task report and record the commit tested.

```sh
.venv/bin/python scripts/golden.py compare cases/g4-contract1
```

The command runs the application check on every accepted section, regenerates the canonical Word and Excel outputs once, and compares them with each accepted pair. It verifies the hashes of the frozen exports first. Word comparisons cover body paragraphs in order (including tables) and comment texts, authors, and anchors. Excel comparisons cover sheet names/order and populated cells by address, including formulas. A difference produces exit status 1 and prints both versions. Save the complete output, explain each unexpected difference, and get Claude's review before merge. Changes elsewhere in the full report can also appear against a section's snapshot. A clean export comparison establishes that the stored report still renders the same content. Skill behavior requires a separate authorized model run.

## Recording an accepted section

Capture the existing canonical export immediately after the accepted section has been promoted and exported, before another export. Use the actual producing commit, acceptance date, task number and model names recorded in the run logs. Include `--export-task` if the canonical promotion/export was a later task.

```sh
.venv/bin/python scripts/golden.py record cases/g4-contract1 "$section" \
    --commit "$producer_commit" --task "$task" --date "$date" --model "$phase_and_model"
```

Repeat `--model` for additional roles. Both original files are stored under `work/golden/<section>/`. Its `manifest.json` identifies their SHA-256 hashes and the packaged skills at the producing commit. When earlier phases used older skills, keep trace-derived provenance alongside the manifest. All are private case data. The command refuses to overwrite an existing baseline. When a replacement checkpoint for the same section is explicitly accepted, preserve its prior directory under a new case archive path before recording its replacement. Never use a candidate regression output to reset an unexplained difference.
