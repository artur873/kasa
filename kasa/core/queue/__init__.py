"""The review queue (briefing, step 7): one card = one decision, on the phone.

* ``build`` - the state of the queue (``queue_state``) from open suggestions (rank 1-3 per subject),
  sensitive subjects, cost object candidates, rule candidates, rules ready for auto and audit samples,
  ordered by what blocks the metrics and costs most when wrong; sentinels every ~50 cards (repeats of the
  person's own decisions older than 7 days);
* ``cards`` - what a card shows (facts, 1-3 proposals as buttons with evidence, badge with precedents,
  never a percentage - decision D8);
* ``actions`` - the buttons (Akceptuj, alternatywa, Inne…, Odrzuć, Odłóż, paczka, cofnij), history, KPI;
* ``state`` - shown / decided / deferred (time of decision, alarm < 0.8 s on "low", < 1.5 s without a badge).
"""
