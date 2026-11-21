# Skill replay

Each accepted section has a private pack at `<case>/work/replay/<section>/`. It stores the exact leading human messages from each original phase transcript, including multimodal content and the second instruction in verification. The skill under test comes from the current checkout. The pack freezes the original case data and pre-phase files and keeps its expected results outside the model-visible copy.

## Run

Select the exact section and one authorized batch. Each phase uses its own frozen pre-phase files. A review starts with its frozen source check even if verification in the same batch saved nothing.

```sh
.venv/bin/python -m reportagent replay cases/g4-contract1 --section "$section" --batch verify-review
.venv/bin/python -m reportagent replay cases/g4-contract1 --section "$section" --batch clarify-revise
```

| Batch | First phase | Second phase | Maximum batch allocation |
|---|---|---|---:|
| verify-review | verify-sources (90,000) | review (100,000) | 190,000 |
| clarify-revise | clarify (100,000) | revise (130,000) | 230,000 |

These limits match the explicit phase caps used in production runs. Each phase is also limited by the remaining batch allocation. For a single phase, use `--skill` with one of `verify-sources`, `review-section`, `clarify-section` or `write-section`. Supply `--section` for a batch or when the case contains several packs.

By default the production and replay commands build the leading messages from their respective case files. Add `--historical-input` to replay the recorded leading messages. The CLI verifies every selected fixture before invoking the model. It preserves `results.json`, transcripts and request sidecars in a new `reportagent-replay-*` directory beside the real case. Each result records `input_mode` and `phase_cap`. A `blocked_before_send` result means that the estimated input and output reserve did not fit.

## Measured outcomes

Each metric includes the historical baseline, observed value and direction. More expected source IDs saved, successful edits, actual saves, first-response saves and matching F/grade statements are favorable. Fewer duplicate numbers, F/grade discrepancies, rejected edits, submitted attempts and tokens are favorable. Attempt count measures workload. Read it alongside successful work and target saves. A cheaper run that does no work can still have worse coverage.

- Verification counts distinct expected IDs only following a successful save of the phase target. Extra and missing IDs are listed.
- The revision metric “edits touching finding lines” counts a distinct finding once if the saved section changed at least one of its frozen, cited original lines. Original-to-final line differences exclude pure insertions between unchanged lines and reverted changes. The measure tracks editing at the cited location. A reviewer decides whether it resolved the finding.
- The existing clarification-number inventory and F/grade comparison supply cross-file counts. If there are zero comparable statements, zero agreements provide no evidence of agreement.
- Native tool results are paired with calls within the same response. Submitted edits without a result are identified separately. A first-response save requires a successful tool result for the correct target.
- Reasoning tokens are part of total tokens. An absent provider count yields `unknown` for that comparison. Baselines are observed historical runs, possibly under a different cap, skill or input layout. One changed number cannot establish which factor caused it.

## Freeze an accepted phase offline

Start from that phase's actual pre-run case. Prepare a directory containing its original `work/` and `out/`. Include input edits and earlier phase outputs that were already present when that phase started. For verification, provide a JSON object containing the original list of expected checklist IDs as `expected_ids`. For revision, supply `finding_lines`, mapping the finding IDs to cited one-based line numbers in that exact starting section. Review and clarify use `{}`. Keep the input trace and notes on how the pre-state was reconstructed with the case evidence.

```sh
.venv/bin/python scripts/freeze_replay.py "$pack" "$phase" "$historical_case" "$prepared_state" "$transcript" "$criteria"
```

The section directory's name must match the historical section filename stem. The freezer captures the human messages verbatim and reproduces successful recorded filesystem operations on a disposable copy to measure the baseline. It records the source-trace digest, frozen file hashes and saved target digest. Existing phase packs are preserved. Verify the outcome hash against the accepted artifact or the original phase's successful write before publishing the pack. Prepare all four phases in a staging directory and move the completed section pack into the canonical replay directory. Frozen project data stays outside Git.
