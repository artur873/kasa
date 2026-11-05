"""Semantics (phase 3): what a model reads from texts, behind one gate.

* ``gate.guarded_complete`` - the only way to a model in the whole code base
  (sensitive words -> pseudonymization -> leak check -> call -> log -> validation);
* ``titles`` - intent and entities of a transfer title;
* ``lines`` - entities of an invoice line or an internal sheet row;
* ``run.run_semantics`` - one pass over a company (idempotent, with a cost estimate);
* ``prompts`` - prompt texts and versions; ``mock`` - the local heuristic model.
"""
