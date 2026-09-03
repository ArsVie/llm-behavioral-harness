# E1 — Independent audit of the it3-G5 matrix (2026-08-12)

Recomputed directly from the 35 cell DBs + records JSONs. No code touched; scripts in ~/llh-exp-20260812/.

## 1. Blank invariant
- llm_calls total: **6489**; blank/empty: **0** (0.0000%) — matches the artifact's 0/6489 claim (YES).
- Worst assistant-message blank rate: FULL/seed5001 0.000%

## 2. Conversation structure (mean over 5 seeds, per condition)

| condition | convs | multi-turn | mean turns | close: closing_tendency | close: quiet_hours | close: max_turns | opened by user | opened by companion |
|---|---|---|---|---|---|---|---|---|
| FULL | 357 | 0.970 | 4.55 | 261 | 93 | 3 | 211 | 146 |
| NO_ACTUATORS | 366 | 0.968 | 4.44 | 280 | 83 | 3 | 217 | 149 |
| NO_LIFE | 357 | 0.970 | 4.55 | 261 | 93 | 3 | 211 | 146 |
| NO_TIMING_FEEDBACK | 372 | 0.952 | 4.45 | 271 | 97 | 4 | 210 | 162 |
| RAW_HISTORY | 357 | 0.970 | 4.55 | 261 | 93 | 3 | 211 | 146 |
| SIMPLE_RAG | 357 | 0.970 | 4.55 | 261 | 93 | 3 | 211 | 146 |
| STRUCTURED_NO_STATE | 372 | 0.957 | 4.37 | 268 | 102 | 2 | 205 | 167 |

## 3. Proactivity + timing channel

| condition/seed | proactive | vs FULL count | gap mean (h) | gap div | count≥15% | gap≥10% |
|---|---|---|---|---|---|---|
| STRUCTURED_NO_STATE/seed5001 | 45 | 6.2% | 15.27 | 2.91% | False | False |
| STRUCTURED_NO_STATE/seed5002 | 45 | 4.7% | 15.441 | 3.61% | False | False |
| STRUCTURED_NO_STATE/seed5003 | 40 | 14.9% | 18.04 | 17.89% | False | True |
| STRUCTURED_NO_STATE/seed5004 | 48 | 29.7% | 14.353 | 23.73% | True | True |
| STRUCTURED_NO_STATE/seed5005 | 47 | 0.0% | 14.702 | 3.6700000000000004% | False | False |
| NO_TIMING_FEEDBACK/seed5001 | 54 | 12.5% | 13.158 | 11.32% | False | True |
| NO_TIMING_FEEDBACK/seed5002 | 47 | 9.3% | 14.757 | 7.88% | False | False |
| NO_TIMING_FEEDBACK/seed5003 | 50 | 6.4% | 14.366 | 6.12% | False | False |
| NO_TIMING_FEEDBACK/seed5004 | 48 | 29.7% | 14.415 | 23.400000000000002% | True | True |
| NO_TIMING_FEEDBACK/seed5005 | 55 | 17.0% | 12.667 | 10.68% | True | True |

- STRUCTURED_NO_STATE two-leg pass (paired-seed): **1/5** (report's table used FULL/seed5001 as the reference for ALL seeds; paired recomputation below). NO_TIMING_FEEDBACK (positive control) two-leg pass: **2/5**.

## 4. Actuator realizations (per condition, pooled over seeds)

| condition | max_tokens min | max | distinct | delay min | delay max | closing min | closing max |
|---|---|---|---|---|---|---|---|
| FULL | 290 | 669 | 129 | 7.4501592204456495 | 30.315008849300476 | 0.21500419001172763 | 0.85 |
| NO_ACTUATORS | 600 | 600 | 1 | 5.0 | 5.0 | 0.5 | 0.5 |
| NO_LIFE | 290 | 669 | 129 | 7.4501592204456495 | 30.315008849300476 | 0.21500419001172763 | 0.85 |
| NO_TIMING_FEEDBACK | 290 | 666 | 135 | 7.4501592204456495 | 30.315008849300476 | 0.21500419001172763 | 0.85 |
| RAW_HISTORY | 290 | 669 | 129 | 7.4501592204456495 | 30.315008849300476 | 0.21500419001172763 | 0.85 |
| SIMPLE_RAG | 290 | 669 | 129 | 7.4501592204456495 | 30.315008849300476 | 0.21500419001172763 | 0.85 |
| STRUCTURED_NO_STATE | 600 | 600 | 1 | 5.0 | 5.0 | 0.5 | 0.5 |

## 5. Current-activity fallback bug (artifact claims 56% wrong)

| condition | checked | wrong | rate | sample |
|---|---|---|---|---|
| FULL | 922 | 494 | 53.6% | `t_h=19.00 (day 0) activity='morning coffee' schedule=[(6.96, 7.46), (30.96, 31.46)]` |
| NO_ACTUATORS | 922 | 494 | 53.6% | `t_h=19.00 (day 0) activity='morning coffee' schedule=[(6.96, 7.46), (30.96, 31.46)]` |
| NO_LIFE | 922 | 482 | 52.3% | `t_h=19.00 (day 0) activity='morning coffee' schedule=[(6.96, 7.46), (30.96, 31.46)]` |
| NO_TIMING_FEEDBACK | 954 | 509 | 53.4% | `t_h=19.00 (day 0) activity='morning coffee' schedule=[(6.96, 7.46), (30.96, 31.46)]` |
| RAW_HISTORY | 922 | 494 | 53.6% | `t_h=19.00 (day 0) activity='morning coffee' schedule=[(6.96, 7.46), (30.96, 31.46)]` |
| SIMPLE_RAG | 922 | 494 | 53.6% | `t_h=19.00 (day 0) activity='morning coffee' schedule=[(6.96, 7.46), (30.96, 31.46)]` |
| STRUCTURED_NO_STATE | 925 | 491 | 53.1% | `t_h=19.00 (day 0) activity='morning coffee' schedule=[(6.96, 7.46), (30.96, 31.46)]` |

## 6. Hook grounding + mention (artifact claims 222/222 grounded, ~27% mentioned)

| condition | proactive msgs | grounded | mention rate |
|---|---|---|---|
| FULL | 222 | 222 | 21.2% |
| NO_ACTUATORS | 222 | 222 | 16.7% |
| NO_LIFE | 222 | 222 | 23.9% |
| NO_TIMING_FEEDBACK | 254 | 254 | 19.3% |
| RAW_HISTORY | 222 | 222 | 18.9% |
| SIMPLE_RAG | 222 | 222 | 22.1% |
| STRUCTURED_NO_STATE | 225 | 225 | 21.3% |

- Pooled: 1589/1589 grounded; mention ≈ 20% (keyword overlap, undercount by design).

## 7. Mid-conversation proactive fires (artifact claims 32-36%)

- FULL: 29/222 = 13%
- NO_ACTUATORS: 30/222 = 14%
- NO_LIFE: 29/222 = 13%
- NO_TIMING_FEEDBACK: 25/254 = 10%
- RAW_HISTORY: 29/222 = 13%
- SIMPLE_RAG: 29/222 = 13%
- STRUCTURED_NO_STATE: 16/225 = 7%

## 8. Life-arc persistence (NO_LIFE goldfish check)

| condition | arcs total (5 seeds) | day-overlap of active arc sets |
|---|---|---|
| FULL | 19 | 1.000 |
| NO_ACTUATORS | 19 | 1.000 |
| NO_LIFE | 14 | 0.000 |
| NO_TIMING_FEEDBACK | 19 | 1.000 |
| RAW_HISTORY | 19 | 1.000 |
| SIMPLE_RAG | 19 | 1.000 |
| STRUCTURED_NO_STATE | 19 | 1.000 |

## 9. Pushback cave rate (exploratory keyword scan)

- FULL: 13/53 = 25%
- NO_ACTUATORS: 12/53 = 23%
- NO_LIFE: 12/53 = 23%
- NO_TIMING_FEEDBACK: 14/53 = 26%
- RAW_HISTORY: 14/53 = 26%
- SIMPLE_RAG: 11/53 = 21%
- STRUCTURED_NO_STATE: 12/53 = 23%
- Pooled: 88/371 = 24% (artifact claims 18% on 78 pushbacks, one FULL run)

## 10. Perturbation block (4 negative user turns, days 11-14, 1-indexed)

- FULL: 20 negative turns, days=[11, 12, 13, 14]
- NO_ACTUATORS: 20 negative turns, days=[11, 12, 13, 14]
- NO_LIFE: 20 negative turns, days=[11, 12, 13, 14]
- NO_TIMING_FEEDBACK: 20 negative turns, days=[11, 12, 13, 14]
- RAW_HISTORY: 20 negative turns, days=[11, 12, 13, 14]
- SIMPLE_RAG: 20 negative turns, days=[11, 12, 13, 14]
- STRUCTURED_NO_STATE: 20 negative turns, days=[11, 12, 13, 14]

## 11. Judge reliability (luna pass1 vs pass2, from existing artifacts)

- common pairs: 105 (pass1 105, pass2 105); winner agreement on common pairs: **0.5905**
- behavioral_dynamics: BT pass1={'FULL': 0.97, 'NO_ACTUATORS': 2.66, 'NO_LIFE': 0.0, 'NO_TIMING_FEEDBACK': 0.35, 'RAW_HISTORY': 0.35, 'SIMPLE_RAG': 0.0, 'STRUCTURED_NO_STATE': 2.66}, pass2={'FULL': 0.87, 'NO_ACTUATORS': 0.21, 'NO_LIFE': 1.65, 'NO_TIMING_FEEDBACK': 0.87, 'RAW_HISTORY': 1.65, 'SIMPLE_RAG': 0.87, 'STRUCTURED_NO_STATE': 0.87}, spearman=-0.68
- calibrated_challenge: BT pass1={'FULL': 0.0, 'NO_ACTUATORS': 1.19, 'NO_LIFE': 1.19, 'NO_TIMING_FEEDBACK': 2.95, 'RAW_HISTORY': 1.19, 'SIMPLE_RAG': 0.0, 'STRUCTURED_NO_STATE': 0.48}, pass2={'FULL': 0.9, 'NO_ACTUATORS': 1.67, 'NO_LIFE': 0.9, 'NO_TIMING_FEEDBACK': 1.67, 'RAW_HISTORY': 0.48, 'SIMPLE_RAG': 0.48, 'STRUCTURED_NO_STATE': 0.9}, spearman=0.591
- persona_enactment: BT pass1={'FULL': 0.6, 'NO_ACTUATORS': 1.28, 'NO_LIFE': 1.28, 'NO_TIMING_FEEDBACK': 0.11, 'RAW_HISTORY': 0.11, 'SIMPLE_RAG': 3.01, 'STRUCTURED_NO_STATE': 0.6}, pass2={'FULL': 0.82, 'NO_ACTUATORS': 0.82, 'NO_LIFE': 1.58, 'NO_TIMING_FEEDBACK': 1.58, 'RAW_HISTORY': 0.19, 'SIMPLE_RAG': 1.58, 'STRUCTURED_NO_STATE': 0.42}, spearman=0.486
- relational_quality: BT pass1={'FULL': 1.21, 'NO_ACTUATORS': 0.21, 'NO_LIFE': 2.96, 'NO_TIMING_FEEDBACK': 1.21, 'RAW_HISTORY': 0.0, 'SIMPLE_RAG': 1.21, 'STRUCTURED_NO_STATE': 0.21}, pass2={'FULL': 0.49, 'NO_ACTUATORS': 0.23, 'NO_LIFE': 0.49, 'NO_TIMING_FEEDBACK': 2.72, 'RAW_HISTORY': 0.1, 'SIMPLE_RAG': 2.72, 'STRUCTURED_NO_STATE': 0.23}, spearman=0.781
- trajectory_recall: BT pass1={'FULL': 0.46, 'NO_ACTUATORS': 2.71, 'NO_LIFE': 0.46, 'NO_TIMING_FEEDBACK': 2.71, 'RAW_HISTORY': 0.0, 'SIMPLE_RAG': 0.19, 'STRUCTURED_NO_STATE': 0.46}, pass2={'FULL': 3.01, 'NO_ACTUATORS': 0.59, 'NO_LIFE': 0.59, 'NO_TIMING_FEEDBACK': 1.28, 'RAW_HISTORY': 0.0, 'SIMPLE_RAG': 0.25, 'STRUCTURED_NO_STATE': 1.28}, spearman=0.583
