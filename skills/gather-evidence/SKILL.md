---
name: gather-evidence
description: Gather cited evidence and missing facts for the section requested in the commission.
---
If /commission/澄清答复.md exists, read it first and record in the notes every answer that concerns this section.
When this section's notes file already exists, read it next, keep everything in it, and continue with the pages it lists as not yet read before searching again.
Do not read other sections' notes; read the source pages they cite instead.
When the section sets out a method from a guide or standard, read that method's tables first (search the page-NNN.txt files for the table numbers) and copy into the notes every indicator with all its bands, the weight rule and the grade thresholds. When the section only applies a method set out in another section, do not gather the method again.
If the notes of the section that sets out the method already exist in /work/notes/ (for example 03-评估流程和方法.md for a slope), do not search for or read the method's tables or the guide's method pages; gather only this section's own facts.
When the plan gives scope criteria for items of this kind, record in the notes this item's own values for each criterion (for example height and slope type), from every source page that gives them, and say whether they meet the criteria; do not rely on any grouped conclusion in the plan.
To find a table, grep the page-NNN.txt files for its number (for example 表3-2 or A.3-1); do not navigate by the contents page, because printed page numbers differ from page-NNN numbers.
Read each table page whole, without a line limit, and continue to the next page until the table ends.
When a table reaches the page bottom, open the next page before saying a row or item is absent.
Source pages are under /sources/; /index.md lists them.
Read the files in /commission/ for this report's scope and rules.
Copy into the notes, once, the commission's rule on which source prevails when sources disagree, with its citation.
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
Before writing that a source lacks a fact, open in full every page that a search hit for this item points to; a search snippet is not a reading.
Write MISSING only when the sources do not contain the fact, not when you have not yet read the page that would contain it.
After saving the notes, keep reading the pages for items you have not yet collected and update the notes; stop when every item is collected or truly missing.
If a page's text is empty, jumbled or a table you cannot follow, read its page image.
