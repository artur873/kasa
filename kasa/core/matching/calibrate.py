"""Calibration of the matching score on a labeled control set (briefing, step 4 and gate 4).

The additive score ``raw`` of a candidate becomes a probability ``p = σ((raw - bias) / temperature)``.
Calibration evaluates every labeled transaction of a control set (``control_set`` of kind
``transaction``, e.g. from ``control-set import-ground-truth``) as if undecided, and tries a grid of
(temperature, bias, auto threshold). For each point it counts the automatic decisions the engine
would make and how many of them disagree with the labels:

1. only points with **0 false automatic matches** qualify (better fewer automata than one false);
2. among them: the most automatic decisions;
3. then the best calibration of the probability (log-loss of all proposed candidates against the
   labels), then the higher threshold, then the point closest to the defaults.

No qualifying point -> the automation is switched off (threshold above 1) until a better
calibration. Every evaluated point goes to ``calibration_result`` (one ``run_id``), the chosen one
with ``chosen = true``; the engine reads the chosen parameters of the latest run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.matching import auto
from kasa.core.matching import weights as W
from kasa.core.matching.engine import Candidate, Evaluation, evaluate, rank

TEMPERATURES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
BIASES = (-1.0, -0.5, 0.0, 0.5, 1.0)
THRESHOLDS = (0.995, 0.997, 0.999)
AUTO_OFF = 1.01
SUBTYPE_EQUIVALENT = {"interest": "fee", None: "other"}   # K20: interest = fee in the labels; unknown = other


@dataclass
class PointMetrics:
    params: dict[str, float]
    autos: int = 0
    false_autos: int = 0
    queue: int = 0
    queue_rank1_agree: int = 0
    labeled: int = 0
    logloss: float = 0.0
    brier: float = 0.0
    false_examples: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "autos": self.autos, "false_autos": self.false_autos, "queue": self.queue,
            "queue_rank1_agree": self.queue_rank1_agree, "labeled": self.labeled,
            "logloss": round(self.logloss, 6), "brier": round(self.brier, 6),
            "false_examples": self.false_examples[:10],
        }


@dataclass
class CalibrationReport:
    run_id: UUID
    control_set_id: UUID
    chosen: PointMetrics
    points: list[PointMetrics]
    auto_off: bool

    def summary(self) -> str:
        p, m = self.chosen.params, self.chosen
        head = (f"Kalibracja ({len(self.points)} zestawów parametrów, zbiór {self.control_set_id}, "
                f"etykiet {m.labeled}): ")
        if self.auto_off:
            return head + "żaden zestaw nie daje 0 fałszywych automatów - automat WYŁĄCZONY (próg > 1)."
        return (head + f"wybrano temperatura {p['temperature']}, przesunięcie {p['bias']}, próg {p['auto_threshold']}"
                f" -> automatów {m.autos}, fałszywych {m.false_autos}, kolejka {m.queue} (pierwsza propozycja zgodna "
                f"z etykietą: {m.queue_rank1_agree}/{m.queue}), log-loss {m.logloss:.4f}")


def labels_of(conn: psycopg.Connection, control_set_id: UUID) -> dict[UUID, dict[str, Any]]:
    """Labels of a transaction control set: transaction id -> label (documents resolved to current heads)."""
    kind = conn.execute("SELECT kind::text FROM control_set WHERE id = %s", (control_set_id,)).fetchone()
    if kind is None:
        raise LookupError(f"Nie ma zbioru kontrolnego {control_set_id}")
    if kind[0] != "transaction":
        raise ValueError(f"zbiór {control_set_id} jest rodzaju {kind[0]} - kalibracja dopasowania wymaga transaction")
    heads = dict(conn.execute("SELECT document_id, head_id FROM document_lineage").fetchall())
    out = {}
    for subject_id, label in conn.execute(
        """
        SELECT subject_id, label FROM control_set_item
         WHERE set_id = %s AND subject_type = 'transaction' AND label IS NOT NULL
        """,
        (control_set_id,),
    ).fetchall():
        docs = []
        for ref in label.get("documents") or []:
            ref_id = UUID(str(ref["id"]))
            docs.append((ref["type"], heads.get(ref_id, ref_id) if ref["type"] == "document" else ref_id))
        out[subject_id] = {**label, "_targets": set(docs)}
    return out


def agrees(cand: Candidate, label: dict[str, Any]) -> bool:
    """Does a candidate say what the label says (status, documents, subtype)?"""
    status = label.get("status")
    if cand.final_status != status:
        return False
    if status == "matched":
        if label.get("other_document") and not label.get("_targets"):
            return False
        return {i.target for i in cand.items} == label["_targets"]
    if status == "no_document":
        return SUBTYPE_EQUIVALENT.get(cand.subtype, cand.subtype) == SUBTYPE_EQUIVALENT.get(
            label.get("subtype"), label.get("subtype"))
    return True


def score_point(evaluation: Evaluation, labels: dict[UUID, dict[str, Any]], params: dict[str, float]) -> PointMetrics:
    metrics = PointMetrics(dict(params))
    rules = evaluation.ctx.builtin_rules
    losses = []
    for result in evaluation.results:
        label = labels.get(result.tx.id)
        if label is None:
            continue
        metrics.labeled += 1
        ranked = rank(list(result.considered), params)
        if not ranked:
            continue
        for cand in ranked:
            y = 1.0 if agrees(cand, label) else 0.0
            p = min(max(cand.score, 1e-9), 1 - 1e-9)
            losses.append((-(y * math.log(p) + (1 - y) * math.log(1 - p)), (p - y) ** 2))
        ok, _ = auto.eligibility(ranked[0], params, rules)
        if ok:
            metrics.autos += 1
            if not agrees(ranked[0], label):
                metrics.false_autos += 1
                metrics.false_examples.append(result.tx.label)
        else:
            metrics.queue += 1
            metrics.queue_rank1_agree += agrees(ranked[0], label)
    if losses:
        metrics.logloss = sum(x for x, _ in losses) / len(losses)
        metrics.brier = sum(y for _, y in losses) / len(losses)
    return metrics


def _preference(m: PointMetrics) -> tuple:
    p = m.params
    return (-m.autos, round(m.logloss, 6), -p["auto_threshold"], abs(p["temperature"] - 1.0), abs(p["bias"]))


def calibrate(conn: psycopg.Connection, company_id: UUID, control_set_id: UUID, *, record: bool = True,
              temperatures=TEMPERATURES, biases=BIASES, thresholds=THRESHOLDS) -> CalibrationReport:
    """Choose (temperature, bias, threshold) with 0 false automatic matches on the control set."""
    labels = labels_of(conn, control_set_id)
    if not labels:
        raise ValueError("zbiór kontrolny nie ma etykiet - najpierw etykietowanie albo import-ground-truth")
    evaluation = evaluate(conn, company_id, subject_ids=set(labels), include_decided=True,
                          params=dict(W.DEFAULT_PARAMS))
    points = []
    for temperature in temperatures:
        for bias in biases:
            for threshold in thresholds:
                params = {**W.DEFAULT_PARAMS, "temperature": temperature, "bias": bias,
                          "auto_threshold": max(threshold, W.MIN_AUTO_THRESHOLD)}
                points.append(score_point(evaluation, labels, params))
    safe = [m for m in points if m.false_autos == 0]
    auto_off = not safe
    if auto_off:
        best = min(points, key=lambda m: (m.false_autos, _preference(m)))
        chosen = score_point(evaluation, labels, {**best.params, "auto_threshold": AUTO_OFF})
        points.append(chosen)
    else:
        chosen = min(safe, key=_preference)
    run_id = uuid4()
    if record:
        for m in points:
            conn.execute(
                """
                INSERT INTO calibration_result (run_id, company_id, control_set_id, kind, params, metrics, chosen)
                VALUES (%s, %s, %s, 'match', %s, %s, %s)
                """,
                (run_id, company_id, control_set_id, Jsonb(m.params),
                 Jsonb({**m.as_json(), "auto_off": auto_off if m is chosen else False}), m is chosen),
            )
    return CalibrationReport(run_id, control_set_id, chosen, points, auto_off)
