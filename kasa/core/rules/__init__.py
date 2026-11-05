"""Rules learned from a person's clicks (briefing, step 6; phase 4, part C).

* ``apply.applicable_rules`` - which rules hit a subject (the contract of phase 4: 4A and 4B call it);
* ``mine`` - candidates from a person's decisions and hard identifiers (never from the automation);
* ``lifecycle`` - a person's decisions about a rule (accept, correct, reject, enable auto, suspend, resume);
* ``observe`` - after every decision of a person: "would the rule have done what you did?";
* ``audit`` - 2% of the automation's decisions a month (at least 20) back to a person;
* ``auto`` - category decisions of rules in stage ``auto`` (matching and allocation apply their own).

Stages hint -> shadow -> ready -> dormant are derived from observations (view ``rule_stage_effective``).
"""
