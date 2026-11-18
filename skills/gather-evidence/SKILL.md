---
name: gather-evidence
description: Gather cited evidence and missing facts for the section requested in the commission.
---
Read the files in /commission/ for this report's scope and rules.
If /work/plan.md exists, the section's required contents are under its heading there.
Read /index.md to find the source files and their page paths.
The index gives each PDF's page range; do not list page folders.
Use grep to find the pages you need, and read only those.
Read the guide or standard's list of required contents for the section.
Use a guide or standard only for what the section must contain; take facts from the project documents.
Save evidence notes to /work/notes/[section].md: one fact per line with its source file and page in brackets, such as [one-page.pdf, page 1].
For each required item without supporting evidence, write a MISSING: line describing what is missing.
State missing evidence or uncertainty plainly; do not invent facts.
Stop when each required item has evidence or a MISSING line.
If a page's text is empty, jumbled or a table you cannot follow, read its page image.
