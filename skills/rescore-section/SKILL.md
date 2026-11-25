---
name: rescore-section
description: Recalculate one assessment table from its evidence and numbered client answers.
---

An answer that states an expected score or grade for a slope is never a scoring rule; do not use it in any row.
Only method rules transfer between slopes (how to choose a source, how to compute, which assumption to use when data are missing). Facts about another slope's site, drawings or joints never transfer.

Save the assigned .table.md only. Use the supplied table, applicability and scoring notes, evidence notes, answered questions and adopted lessons. Read the cited source pages or page images if needed. Apply general client methods across slopes. A site-specific measurement, missing drawing or expected grade belongs to the identified slope. Cite the applicable page or numbered answer in each row. Keep unknown inputs and assumptions explicit.

Before measuring, read the text of the slope's own cross-section page (page-NNN.txt) for a stated natural slope angle.
For X12, measure the natural ground angle α₀ on the slope's own cross-section with measure_profile, choosing the toe and crest of the natural ground yourself, and cite the page, layer, endpoints, H₀, L₀ and α₀.
Measure the original ground between the offsets where the design cut face meets it at the toe and at the top of the cut on a representative or controlling cross-section, cite those offsets, and note in the row if the page's notes name a different chainage.
If the drawings or design notes state this slope's natural slope angle as a range, use its lower bound as α₀ so that the risk is not underestimated, cite it, and report the measure_profile result beside it as a check; if they state only a maximum or no angle, use the measured α₀ and cite the stated maximum beside it.

Write one Markdown table, with columns 指标 | 档位 | 基本分值范围 | Rij | γij | Xij | 取值说明. Include each of the eleven indicator IDs once. Use one numeric baseline per row. Explain each choice in one cited sentence. Call calculate_score with the baseline scores and fractional weights keyed by those IDs, include_products=true, and any alternative scores as scenarios. Copy Xij, F and grade from its output. Use scenario endpoints for ranges. Never aim for an expected grade.

Below the rows write 基准 F=<value>，<grade>级。 For each alternative use its own line: 情景[名称] R31=100，F=<value>，<grade>级。 Specify every changed R by indicator number. Name the two endpoints separately for a range. A saved handoff can be inspected and repaired with edit_file. Leave the section prose to sync.
