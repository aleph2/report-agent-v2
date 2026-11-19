---
name: plan-report
description: Plan the requested report structure and cited section requirements.
---
Read every file in /commission/; they define this report's scope, format and outline.
Read /index.md.
Files in /reference/ show the expected structure, layout, style and the wording of fixed chapters; never take project facts or values from them.
Write and save /work/plan.md first, from the commission alone: one "## <nn>-<name>" heading per section, in the commission's order; under each, the commission's guidance for that section and the sources it names.
Begin it with --- frontmatter: title: <report title>, project: <project name>, and one section: <heading level>|<section filename stem> line per heading, in that same order; close with ---.
If the commission gives no outline, take the structure from the guide instead.
After saving, you may read up to five guide pages to add each section's required contents with [file, page N], editing the plan.
Where the commission asks for the same assessment for each item of a kind (for example each slope), find the list of items in the sources' summary tables (up to five source pages) and add one heading per item after that section's heading: ## <nn>-<k>-<item>, numbered in source order.
Before adding the item headings, find the scope criteria in the guide or standard the commission names (grep the page-NNN.txt files for its applicability clause, for example 适用 or 范围, and any height or type limits), and check each item against them with the item's own source values.
Add a heading only for items within scope. For each item left out, add one line under the section heading: the item, the criterion it fails, and both citations. Where the sources disagree on whether an item is in scope, or on its range, keep the item and add a 待澄清 line saying what disagrees.
When /work/plan.md already exists, keep its existing section headings and guidance unchanged, add only what is missing, and update the frontmatter order to match.
Reply in one line.
