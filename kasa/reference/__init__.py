"""Readers of reference truth files (not the core): known answers for control sets.

``ground_truth`` reads the format of ``fixtures/<company>/ground_truth.json`` (written by
``fixtures/generate.py``) into labels the core understands (``kasa.core.control_sets``).
Company concepts of that format (its cost objects, its internal sheets) stay here.
"""
