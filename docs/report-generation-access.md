# Mobile report generation by plan

The backend reads `users.plan`: `No Ads Monthly` (Plus), `Pro Monthly`, and
`Pro Yearly` receive paid scope; all other values receive Free scope. Clients
cannot request a paid scope themselves.

| Report | Free generation | Plus / Pro generation |
| --- | --- | --- |
| Player | 2 strengths, 2 weaknesses, 3 role bullets | 3 strengths, 3 weaknesses, 4 role bullets |
| Team analysis | AI-selected titles, metrics and key players; no locked explanations | Attack, defense, strengths and weaknesses commentary |
| Pre-match | Positive and strategy insights only | Also weakness, player and momentum insights |
| Post-match | First 2 team insights in the shared tone ordering; first featured player and development player | 3 team insights; both featured players and development player |

The unused player Usage Recommendation bullet is omitted for every plan.
Empty post-match lock slots preserve the mobile card layout without generating
hidden prose. Numeric evidence remains available for Free users. Team-profile AI selection is shared
between plans; only explanation output is omitted for Free. A Free team analysis
therefore still makes AI calls and does not have a zero AI cost.

Saved section metadata includes `access_tier`. Paid users opening a Free section
regenerate that section at paid scope once; unchanged ready sections are reused.
The mobile app waits for the upgraded scope before displaying the result.
Historical reports without this marker already contain full content and are
reused without additional AI calls; existing UI locks remain applicable.

No SQL migration is required: the marker lives inside existing JSON report data.
Deploy the backend and ship the mobile JavaScript changes together. These rules
apply to saved/lazy reports and the older direct/eager report endpoints.

Validation (mocked AI and database; no paid requests):

```sh
venv/bin/python -m unittest discover -s tests -v
```

These changes reduce generated output and skip entire calls where all prose is
locked. Exact dollar savings still require real token-usage measurements.

## Visible-content audit

- Team strengths/weaknesses and attack/defense profiles retain model-selected
  visible titles, metrics and players. Previously the Free path returned fixed
  fallback selections; that was incorrect and has been removed.
- Player scouting retains AI for every visible bullet with shared length targets.
- Pre-match player/momentum cards select their visible numeric content without AI
  already; only their locked perspective text is skipped. Positive and strategy
  insights retain the original common prompt requirements and word targets.
- Post-match player selection is unchanged. Only the locked second featured
  player's explanation is excluded. Team insights retain the common evidence-led
  tone rules, including two insights of the same tone when justified.
- Team profiles are not persisted: reopening after deployment fetches the corrected
  selections. No database migration is required.
