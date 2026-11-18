---
name: write-section
description: Draft a cited assessment section from the evidence notes in /work/notes/.
---
Read the files in /commission/ for this report's scope and rules.
Ignore /work/runs/; it holds run logs, not evidence.
If /work/plan.md exists, the section's required contents are under its heading there.
Read the evidence notes in /work/notes/ and write the section from those notes.
If no notes file exists, write the section with every required item marked missing.
Write and save the section from the notes alone; do not read source pages.
Write the requested section to /work/sections/<nn>-<name>.md.
Use Markdown with one # heading; use ## and ### for subsections as needed.
After each fact, cite the source file and page recorded in the notes, such as [one-page.pdf, page 1].
Carry every MISSING item from the notes into a final "缺失与待补充事项" list.
Put clarification items in the final list; Word comments and the Excel list are produced at export, so do not say they have been made.
Name only the sources your notes cite.
State missing evidence or uncertainty plainly; do not invent facts.
After saving the section, reply in one line with its path.
