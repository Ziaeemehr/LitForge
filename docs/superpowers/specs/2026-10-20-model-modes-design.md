# Model modes: one choice instead of per-task setup

## Problem

Settings → Models asked for a tier (large / medium / small / Cloud-light), a cloud policy (in Cloud & keys) and,
for anything unusual, task-by-task assignments. A user who only wants "use the cloud" or "stay on this computer"
had to understand all three. Better math (high) silently went to the cheap cloud model in Cloud-light.

## Design

One **mode** sets the tier, the policy and every task's model:

| Mode | Models | Policy | Better math (high) |
|---|---|---|---|
| Local | the hardware's local tier (large / medium / small) for every task | `local_only`; cloud consent off | local vision model |
| Cloud | cheap cloud model for bulk work (screening, extraction, figures, equations, page rewrites); strong cloud model (or the cheap one) for questions, analysis and writing | `hybrid` | cheap cloud model — the page shows its cost per page |
| Hybrid | bulk work on the local tier; questions, analysis and writing on the strong cloud model | `hybrid` | local vision model |
| Manual | unchanged: the user's own task assignments | unchanged | unchanged |

- Bulk tasks: `screen`, `screen_fulltext`, `matrix_extract`, `claim_extract`, `asset_analyze`, `equation_compare`,
  `page_rewrite`. Quality tasks: every other task (`ask`, `audit`, `claim_relation`, `gap_design`, `console_plan`,
  `write_*`).
- Cloud and Hybrid keep the one-time **Enable cloud** consent (applying a mode never turns it on), the dollar limits,
  and approval for sensitive data (the author's drafts). Local turns cloud consent off.
- Assigning a task by hand switches the mode to **Manual**, so a mode never silently overwrites a choice.
- The existing tier path (`tier=` without `mode=`) is unchanged; ownership rules (`setup_owned`) apply to modes too.

## UI

Settings → Models → Setup shows four mode cards with one line each; Cloud and Hybrid show the cloud model pickers
(Cloud: cheap image model + optional strong; Hybrid: the strong model). The tier menu moves under "Advanced". The
Export menu's "Math: high" says when it runs on a cloud model and what a page costs.
