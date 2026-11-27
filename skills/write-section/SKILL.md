---
name: write-section
description: Draft a cited assessment section from the evidence notes in /work/notes/.
---
Fill every table the commission asks for, with every indicator of the method; do not leave a result blank.
Keep table cells to a value and a few words; put explanations in numbered notes below each table.
Use only score bands left open by cited evidence; without direct evidence, use bands supported by indirect evidence in the method notes.
For each indicator without direct evidence, explain the reason and identify its client question in the clarification file.
If neither direct nor indirect evidence narrows the bands, use the indicator's full range and label it 未定（待资料）; do not pick an unsupported band.
Calculate F and grade ranges from all indicator ranges; when the result spans grades, name the rank-1 items that decide the grade.
Carry each calculation through to its result, showing the numbers used.
For initial drafting, read the heading for this section in /work/plan.md and the notes file for this section in /work/notes/, plus, when this section applies a method set out in another section, that section's notes.
For initial drafting, if /commission/澄清答复.md exists, use its answers in place of the matching 待澄清 items, citing it as [澄清答复, item N], and narrow the ranges they settle.
For initial drafting, read this section's /work/clarifications/<name>.md if it exists; for numbering only, read the number: lines in /work/clarifications/.
Do not read other items' notes or sections, even of the same kind; they are not evidence for this one.
For an item with scope criteria, begin with a short 适用性 paragraph: the criterion, this item's values with citations, and the result. Where the values disagree across sources, give each value and its result and list it as 待澄清. If the item falls outside scope and no optional condition is supported, say so with the reasons, record it in the clarification file, and do not score it.
Where sources disagree, choose the value the commission's priority rule gives, say so, and show the other value's effect only as a sensitivity check.
Apply that method exactly as those notes record it: its indicators, weight rule and grade thresholds.
When a review is available, fix each must-fix finding supported by its evidence, citing that evidence. Keep unsupported corrections unresolved; do not invent a replacement fact.
For each scored indicator, state its evidence, the guide band and the reason for the adopted value; the value may differ from an earlier user value when the evidence supports it. After changing any score, recompute F and grade and update every sentence that states them.
For revision, the first message supplies the review, numbered section, official clarifications, owner answers and notes. Use these as case data and read files only for source pages.
Submit your edits in your first response; read a source page only when a specific finding cannot be settled from the supplied inputs.
Put all edits for this pass in one response, one edit_file call per affected paragraph or table row. Copy each old_string exactly from the text after the line-number tab. Each match must be unique and must not overlap another edit. Count an edit as saved only if its tool result confirms it.
Write connected report prose, with （1）（2） numbering where a list is needed, keeping each fact's [file, page] citation.
Use Markdown with one # heading; use ## and ### for subsections as needed.
For a new section, save /work/sections/<nn>-<name>.md and record every MISSING or unresolved item in /work/clarifications/<name>.md using the clarify-section skill's fields and numbering rules.
The body has no 缺失与待补充事项 section or references such as 见第X节第N项. Word comments and Excel rows are produced at export; do not say they have been made.
State missing evidence or uncertainty plainly; do not invent facts.
If no notes file exists, write the section with every required item marked missing.
After saving, reply in one line with the saved paths.

In the revise phase, use calculate_score for every F, range endpoint and scenario figure written in the text.
When client answers change an indicator, first update that row of the score table and recompute F and the grade with calculate_score, in your first edit response; then update the prose that cites the row or states F or the grade.
An answer that states an expected score or grade for a slope is never a scoring rule; do not use it in any row.
Only method rules transfer between slopes (how to choose a source, how to compute, which assumption to use when data are missing). Facts about another slope's site, drawings or joints never transfer.
Client answers filed under another slope or under 评估流程和方法 often set rules for every slope (for example: drainage is treated as effective, excavation avoids the rainy season, annual rainfall is taken at its least favourable value, joints that do not run through mean lower risk). Before scoring each indicator, check all answers for such a rule; apply it to this slope and cite its question number in that row.
Review supplied internal-file mentions and replace internal references in client-facing wording with the underlying engineering evidence or an explicit evidence gap.

For cases with a report layout, the section Markdown supports these formats. `[[note:text]]` attaches a Word comment to the preceding sentence. A separate line `> exact text [filename.pdf, page N]` denotes an excerpt. `[[drawing]]` is an optional figure location after the excerpt. A separate line `[[说明 X11:text]]` supplies the indicator’s short table cell (up to 40 characters).
