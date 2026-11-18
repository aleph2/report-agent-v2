---
name: gather-evidence
description: Gather cited evidence and missing facts for the section requested in the commission.
---
When this section's notes file already exists, read it first, keep everything in it, and continue with the pages it lists as not yet read before searching again.
Do not read other sections' notes; read the source pages they cite instead.
When the section applies a method from a guide or standard, read that method's own tables in the source (search the page-NNN.txt files for the table number) and copy into the notes every indicator, its bands and the weight rule.
Read the files in /commission/ for this report's scope and rules.
Ignore /work/runs/; it holds run logs, not evidence.
If /work/plan.md exists, the section's required contents are under its heading there.
Read /index.md to find the source files and their page paths.
The index gives each PDF's page range; do not list page folders.
Use grep to find the pages you need, and read only those.
To find a chapter in /reference/, first read its table-of-contents pages, then read only that chapter's pages.
Search the page-NNN.txt files, not the PDFs.
Read the guide or standard's list of required contents for the section.
Use a guide or standard only for what the section must contain; take facts from the project documents.
Do not take facts from /reference/.
Where the plan marks a part as fixed content (for example 固定内容不变), note the reference's wording for that part with [reference file, page]; take no project facts from the reference.
Create /work/notes/[section].md after your first source page, and add to it after every two or three pages you read; one fact per line with its source file and page in brackets, such as [one-page.pdf, page 1].
For each required item without supporting evidence, write a MISSING: line describing what is missing.
State missing evidence or uncertainty plainly; do not invent facts.
Write MISSING only when the sources do not contain the fact, not when you have not yet read the page that would contain it.
After saving the notes, keep reading the pages for items you have not yet collected and update the notes; stop when every item is collected or truly missing.
If a page's text is empty, jumbled or a table you cannot follow, read its page image.
