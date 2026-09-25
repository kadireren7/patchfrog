# M4 cost benchmark (deterministic, fake provider, synthetic prices)

_deterministic call-shape/cost benchmark with a scripted fake reviewer and synthetic prices -- measures PatchFrog's execution cost, never model review quality_

| scenario | tier (after) | calls before -> after | critic before -> after | input tokens before -> after | est. USD before -> after | findings before/after | target | met |
|---|---|---|---|---|---|---|---|---|
| comment_only | no_ai | 1 -> 0 | 0 -> 0 | 2023 -> 0 | 0.002039 -> 0.000000 | 0/0 | <= 0 | yes |
| docs_only | no_ai | 1 -> 0 | 0 -> 0 | 1742 -> 0 | 0.001758 -> 0.000000 | 0/0 | <= 0 | yes |
| tiny_code | tiny | 1 -> 1 | 0 -> 0 | 1787 -> 1822 | 0.001803 -> 0.001838 | 0/0 | <= 1 | yes |
| normal_correctness_bug | normal | 6 -> 1 | 1 -> 0 | 12064 -> 2906 | 0.013784 -> 0.003426 | 1/1 | <= 2 | yes |
| medium_cross_module | elevated | 4 -> 1 | 0 -> 0 | 7537 -> 2478 | 0.007601 -> 0.002494 | 0/0 | <= 3 | yes |
| auth_sensitive | high_risk | 3 -> 3 | 1 -> 1 | 5197 -> 5245 | 0.006541 -> 0.006589 | 1/1 | <= 5 | yes |
| schema_migration | high_risk | 4 -> 2 | 0 -> 0 | 6969 -> 3938 | 0.007033 -> 0.003970 | 0/0 | <= 5 | yes |
| public_api_change | elevated | 1 -> 1 | 0 -> 0 | 2032 -> 2067 | 0.002048 -> 0.002083 | 0/0 | <= 3 | yes |
| test_only | tiny | 2 -> 1 | 0 -> 0 | 3579 -> 1927 | 0.003611 -> 0.001943 | 0/0 | <= 1 | yes |
| exact_head_repeat | tiny | 0 -> 0 | 0 -> 0 | 0 -> 0 | 0.000000 -> 0.000000 | 0/0 | <= 0 | yes |

Totals: provider calls 23 -> 10; input tokens 42930 -> 20383; est. USD 0.046218 -> 0.022343; accepted findings 2 -> 2.
All targets met: True. All findings preserved: True.
