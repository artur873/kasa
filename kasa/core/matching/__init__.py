"""Matching transfers to invoices (briefing, step 4): engine, suggestions, automation, decisions, calibration.

Entry points: :func:`kasa.core.matching.run.run_matching` (one run of a company),
:func:`kasa.core.matching.decide.apply_match` (the decision on a suggestion),
:func:`kasa.core.matching.calibrate.calibrate` (thresholds on a labeled control set),
:func:`kasa.core.matching.fx.rate_for` (the one exchange-rate function).
"""
