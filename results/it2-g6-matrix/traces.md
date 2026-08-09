---
type: causal-traces
title: "Causal traces — spontaneous proactive messages (plan §13, deliverable 10)"
description: "Machine-generated provenance walks: message -> intent -> source -> parent chain + timing + behavior + memory context"
tags: [traces, auditability]
timestamp: 2026-08-09T00:00:00+00:00
---

# Causal traces

## Trace 1 — message #1 (day 0, t=15.3h)

> 

- OutgoingMessage **1** ()
- ProactiveIntent **pi_agenda_item_ag_0_i_movies_15.300** (reason=schedule, status=fired)
- AgendaItem **ag_0_i_movies** (activity=try a small movies exercise, status=completed)
- IndependentInterest **movies** ()

**Timing:** planned 15.300111583832008 / fired 15.300111583832008 (delay 0.0h), window 15.30..18.30h, schedule fired
**Behavior:** max_tokens=560 delay=3.7003944561711855s closing=0.40973682606692496 initiative=0.40055001523642136 length_scale=0.9326318906070844
**Persisted intent_id:** `pi_agenda_item_ag_0_i_movies_15.300`
**Evidence:** `agenda_item:ag_0_i_movies activity='try a small movies exercise' status=planned source=interest:movies window=15.00..16.00 salience=0.780`

## Trace 2 — message #18 (day 4, t=114.997h)

> The evening’s settling in here — the guitar’s back on its stand, strings still slightly warm. Just letting the day’s quiet parts sit for a m

- OutgoingMessage **18** ()
- ProactiveIntent **pi_agenda_item_ag_4_a_arc_2_114.997** (reason=schedule, status=fired)
- AgendaItem **ag_4_a_arc_2** (activity=finish the current piece, status=completed)
- LifeArc **arc_2** (name=learning metal, interest=metal, status=completed)
- IndependentInterest **metal** ()

**Timing:** planned 114.99687147744174 / fired 114.99687147744174 (delay 0.0h), window 115.00..118.00h, schedule fired
**Behavior:** max_tokens=536 delay=5.495682333398996s closing=0.37003899047994265 initiative=0.2910871323140849 length_scale=0.8934331591007372
**Persisted intent_id:** `pi_agenda_item_ag_4_a_arc_2_114.997`
**Evidence:** `agenda_item:ag_4_a_arc_2 activity='finish the current piece' status=planned source=arc:arc_2 window=112.00..113.12 salience=0.764`

## Trace 3 — message #23 (day 5, t=138.819h)

> 

- OutgoingMessage **23** ()
- ProactiveIntent **pi_agenda_item_ag_5_i_lifting_138.819** (reason=schedule, status=fired)
- AgendaItem **ag_5_i_lifting** (activity=practice lifting, status=completed)
- IndependentInterest **lifting** ()

**Timing:** planned 138.81902942657794 / fired 138.81902942657794 (delay 0.0h), window 138.82..141.82h, schedule fired
**Behavior:** max_tokens=542 delay=5.117518543272823s closing=0.40157248818701846 initiative=0.31828518899746705 length_scale=0.9039919208419124
**Persisted intent_id:** `pi_agenda_item_ag_5_i_lifting_138.819`
**Evidence:** `agenda_item:ag_5_i_lifting activity='practice lifting' status=planned source=interest:lifting window=140.00..141.55 salience=0.935`

## Trace 4 — message #32 (day 7, t=186.6h)

> There's a particular quiet when the bike's away, isn't there? Not the loud kind — more like a missing beat in a song you know by heart. The 

- OutgoingMessage **32** ()
- ProactiveIntent **pi_agenda_item_ag_7_a_arc_1_185.133** (reason=schedule, status=fired)
- AgendaItem **ag_7_a_arc_1** (activity=reflect on how it is going, status=completed)
- LifeArc **arc_1** (name=weekly guitar practice, interest=guitar, status=completed)
- IndependentInterest **guitar** ()

**Timing:** planned 185.1332452373347 / fired 186.6 (delay 1.467h), window 185.13..188.13h, schedule fired
**Behavior:** max_tokens=572 delay=3.6677852397330746s closing=0.2638734422704614 initiative=0.5743738825561086 length_scale=0.954007912393815
**Persisted intent_id:** `pi_agenda_item_ag_7_a_arc_1_185.133`
**Evidence:** `agenda_item:ag_7_a_arc_1 activity='reflect on how it is going' status=planned source=arc:arc_1 window=186.00..188.49 salience=0.442`

## Trace 5 — message #41 (day 9, t=234.5h)

> 

- OutgoingMessage **41** ()
- ProactiveIntent **pi_agenda_item_ag_9_i_literature_234.231** (reason=schedule, status=fired)
- AgendaItem **ag_9_i_literature** (activity=try a small literature exercise, status=completed)
- IndependentInterest **literature** ()

**Timing:** planned 234.23123926219702 / fired 234.5 (delay 0.269h), window 234.23..237.23h, schedule fired
**Behavior:** max_tokens=538 delay=3.948636797012492s closing=0.26277924554357096 initiative=0.4242347668526827 length_scale=0.8961705726859411
**Persisted intent_id:** `pi_agenda_item_ag_9_i_literature_234.231`
**Evidence:** `agenda_item:ag_9_i_literature activity='try a small literature exercise' status=planned source=interest:literature window=234.00..235.12 salience=0.459`
