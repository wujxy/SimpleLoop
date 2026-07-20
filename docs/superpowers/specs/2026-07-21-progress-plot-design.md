# SimpleLoop Per-Round Progress Plot Design

## Goal

Generate one `progress.png` in each run directory and redraw it after every
completed round so a running optimization can be monitored without parsing
`history.jsonl`.

## Output

`progress.png` contains two vertically stacked plots:

1. Score
   - Every candidate is shown as a light scatter point.
   - Selected candidates are connected by a prominent line.
   - The y-axis is fixed to the judger score range `[0, 1]`.
2. Objective
   - Every candidate with a numeric objective is shown as a light scatter point.
   - The accepted incumbent is shown as a prominent step line.
   - A round with no selected candidate carries the prior incumbent value
     forward.
   - The objective key and `lower is better` or `higher is better` direction are
     displayed in the title.

Round numbers are displayed as one-based values to match the run log. Candidate
indexes are not connected across rounds because `c0`, `c1`, and so on do not
represent persistent lineages.

## Architecture

- Add `simpleloop/plot.py`.
- Use Matplotlib's non-interactive `Agg` backend and add Matplotlib as a project
  dependency.
- Treat `history.jsonl` as the only chart data source.
- Provide a pure history-to-series transformation separate from rendering so
  lineage behavior can be tested without inspecting pixels.
- After a round record is appended, redraw the chart to a temporary PNG and use
  an atomic replace to publish `run_dir/progress.png`.
- Plotting applies to automatic parallel self-loop runs, serial/static proposal
  runs, failed rounds, and continued runs.
- Add the image near the top of `final_report.md` using a relative Markdown
  image reference.

## Record Semantics

- Parallel generation:
  - Candidate scatter points come from every item in `candidates`.
  - The selected score comes from `selected_candidate`.
  - The incumbent objective changes only when `selected_candidate` and
    `selected_sha` are present.
- Serial record:
  - The single round record is the candidate scatter point.
  - An accepted record updates the selected score and incumbent objective.
- Missing or non-numeric score/objective values are omitted from their scatter
  series.
- A failed or non-selected round does not erase the existing incumbent line.
- If no incumbent objective has been established yet, the incumbent line stays
  empty until the first accepted numeric objective.

## Failure Handling

- Chart generation is observational and must never terminate the optimization
  loop.
- Import, data, or rendering failures emit a concise warning and leave the last
  valid `progress.png` untouched.
- Temporary files are cleaned up best-effort after a rendering failure.

## Tests

- Transform parallel history into all-candidate scatter series.
- Carry the incumbent through a no-winner generation.
- Support both lower-is-better and higher-is-better labels.
- Transform legacy serial history.
- Skip missing metrics without failing.
- Produce a valid non-empty PNG through the `Agg` backend.
- Preserve the previous image if rendering fails before atomic replacement.
- Verify the final report references `progress.png`.
