---
name: write-section
description: Draft a cited assessment section from the evidence notes in /work/notes/.
---
Fill every table the commission asks for, with every indicator of the method; do not leave a result blank.
Where an indicator has no direct evidence, give its score as a range across the bands the evidence allows, with the reason, and list it as 待澄清; give the total and grade as a range too, from the lowest and highest values.
Carry each calculation through to its result, showing the numbers used.
Read the heading for this section in /work/plan.md and the notes file for this section in /work/notes/, plus, when this section applies a method set out in another section, that section's notes.
If /commission/澄清答复.md exists, use its answers in place of the matching 待澄清 items, citing it as [澄清答复, item N], and narrow the ranges they settle.
Do not read other items' notes or sections, even of the same kind; they are not evidence for this one.
For an item with scope criteria, begin with a short 适用性 paragraph: the criterion, this item's values with citations, and the result. Where the values disagree across sources, give each value and its result and list it as 待澄清. If the item falls outside scope and no optional condition is supported, say so with the reasons, put it in 缺失与待补充事项, and do not score it.
Where sources disagree, choose the value the commission's priority rule gives, say so, and show the other value's effect only as a sensitivity check.
Apply that method exactly as those notes record it: its indicators, weight rule and grade thresholds.
If /work/reviews/<name>.md exists, read it too and fix every must-fix finding, citing the evidence it gives.
Then write and save /work/sections/<nn>-<name>.md at once, from those files alone; do not read any other file.
Write connected report prose, with （1）（2） numbering where a list is needed, keeping each fact's [file, page] citation.
Use Markdown with one # heading; use ## and ### for subsections as needed.
Carry every MISSING item from the notes into a final "缺失与待补充事项" list; Word comments and the Excel list are produced at export, so do not say they have been made.
State missing evidence or uncertainty plainly; do not invent facts.
If no notes file exists, write the section with every required item marked missing.
After saving the section, reply in one line with its path.
