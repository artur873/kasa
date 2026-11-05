"""Metric definitions - every module defines classes derived from ``kasa.core.metrics.base.Metric``.

A change of a definition is a new class (or a new ``version``) with a test; the SHA-256 of the code of every
(key, version) is recorded in ``metrics.metric_definition`` and in ``definitions.lock.json``.
"""
