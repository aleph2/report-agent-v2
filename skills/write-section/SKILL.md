---
name: write-section
description: Draft a cited assessment section from the evidence notes in /work/notes/.
---
Fill every table the commission asks for, with every indicator of the method; do not leave a result blank.
Keep table cells to a value and a few words; put explanations in numbered notes below each table.
Use only score bands left open by cited evidence; without direct evidence, use bands supported by indirect evidence in the method notes.
For each indicator without direct evidence, explain the reason and give it a client question in the clarification file.
If neither direct nor indirect evidence narrows the bands, use the indicator's full range and label it 未定（待资料）; do not pick an unsupported band.
Calculate F and grade ranges from all indicator ranges; when the result spans grades, name the rank-1 items that decide the grade.
Carry each calculation through to its result, showing the numbers used.
Read the heading for this section in /work/plan.md and the notes file for this section in /work/notes/, plus, when this section applies a method set out in another section, that section's notes.
If /commission/澄清答复.md exists, use its answers in place of the matching 待澄清 items, citing it as [澄清答复, item N], and narrow the ranges they settle.
Read this section's /work/clarifications/<name>.md if it exists; for numbering only, read the number: lines in /work/clarifications/.
Do not read other items' notes or sections, even of the same kind; they are not evidence for this one.
For an item with scope criteria, begin with a short 适用性 paragraph: the criterion, this item's values with citations, and the result. Where the values disagree across sources, give each value and its result and list it as 待澄清. If the item falls outside scope and no optional condition is supported, say so with the reasons, record it in the clarification file, and do not score it.
Where sources disagree, choose the value the commission's priority rule gives, say so, and show the other value's effect only as a sensitivity check.
Apply that method exactly as those notes record it: its indicators, weight rule and grade thresholds.
If /work/reviews/<name>.md exists, read it too and fix every must-fix finding, citing the evidence it gives.
For revisions, save /work/clarifications/<name>.md first, filling every client field.
Then use edit_file only on paragraphs and table rows concerned by the review's must-fix findings.
For a new section, write and save /work/sections/<nn>-<name>.md from those files alone; do not read any other file.
Write connected report prose, with （1）（2） numbering where a list is needed, keeping each fact's [file, page] citation.
Use Markdown with one # heading; use ## and ### for subsections as needed.
Save every MISSING or unresolved item in /work/clarifications/<name>.md, one block per item separated by ---: number:, anchor: 「exact body sentence」, content:, sources:. Keep existing numbers; new items use unused numbers, never a retired number.
Write each clarification source as its full file name and page (or item for an unpaged answer file); never use 同上 or same as above, because client rows stand alone.
Also give object: (the client's item name), question: (one direct client question), required: (the settling document or value), effect: (quantified score/grade impact, or descriptive only), and rank: 1 for an indicator without direct evidence or a grade change, 2 for other score-only items, 3 for description.
The body has no 缺失与待补充事项 section or references such as 见第X节第N项. Word comments and Excel rows are produced at export; do not say they have been made.
State missing evidence or uncertainty plainly; do not invent facts.
If no notes file exists, write the section with every required item marked missing.
After saving both files, reply in one line with their paths.
