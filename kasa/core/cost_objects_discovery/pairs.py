"""Pair model (Fellegi-Sunter): are two records about the same cost object?

Every pair of records is compared on six features; each feature lands on a level:

| feature      | levels                          | what is compared                                   |
| ------------ | ------------------------------- | -------------------------------------------------- |
| name         | exact / high / partial / none   | name tokens, overlap weighted by rarity (IDF)      |
| client       | agree / disagree                | client tokens (buyer, payer, resolved hint)        |
| dates        | overlap / near / mid / far      | days between the date windows                      |
| city         | agree / disagree                | city                                               |
| rare_token   | yes / no                        | a shared rare token that is not the client or city |
| persons      | shared / none                   | pseudonyms of people (internal files, transfers)   |

A value missing on either side contributes nothing. The evidence of a level is
``log2(m / u)`` where ``m = P(level | same)`` and ``u = P(level | different)``; the probability
of "same" is the logistic of the prior log-odds plus the sum of the evidence.

No weight lives in the code: m, u, the prior and the grouping thresholds are rows of
``pair_model_weights`` (version 0 = priors of migration 0010). ``retrain`` counts the levels of
pairs labeled by human decisions (approve = same, split / distinct = different, merge = same;
reject carries no pair evidence - it says "no cost object", not "different records") and writes
a new version: m and u are posterior means of a Dirichlet prior (the prior row
with its ``pseudo_count``) updated by the counts ("EM-lite": one counting step on labeled pairs).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.cost_objects_discovery.records import (
    DiscoveryRecord,
    document_frequencies,
)
from kasa.core.cost_objects_discovery.text import window_days, window_gap_days

FEATURES = ("name", "client", "dates", "city", "rare_token", "persons")
LEVELS: dict[str, tuple[str, ...]] = {
    "name": ("exact", "high", "partial", "none"),
    "client": ("agree", "disagree"),
    "dates": ("overlap", "near", "mid", "far"),
    "city": ("agree", "disagree"),
    "rare_token": ("yes", "no"),
    "persons": ("shared", "none"),
}
PRIOR = "__prior__"
THRESHOLD = "__threshold__"
PARAM = "__param__"

# Discretization of the raw comparisons (what a level means, not how much it weighs).
NAME_EXACT, NAME_HIGH, NAME_PARTIAL = 0.85, 0.6, 0.3
DATES_NEAR_DAYS, DATES_MID_DAYS, SHORT_WINDOW_DAYS = 2, 10, 7


@dataclass
class PairModel:
    company_id: UUID | None
    version: int
    m: dict[tuple[str, str], float]
    u: dict[tuple[str, str], float]
    pseudo: dict[tuple[str, str], float]
    prior_match: float
    thresholds: dict[str, float]
    params: dict[str, float]
    trained_on_decisions: int = 0

    def evidence(self, feature: str, level: str) -> float:
        return math.log2(self.m[(feature, level)] / self.u[(feature, level)])

    def log_odds(self, levels: dict[str, str]) -> float:
        prior = math.log2(self.prior_match / (1 - self.prior_match))
        return prior + sum(self.evidence(f, lv) for f, lv in levels.items())

    def probability(self, levels: dict[str, str]) -> float:
        score = self.log_odds(levels)
        if score > 60:
            return 1.0
        if score < -60:
            return 0.0
        return 1.0 / (1.0 + 2.0 ** (-score))

    def threshold(self, name: str) -> float:
        return self.thresholds[name]


def load_model(conn: psycopg.Connection, company_id: UUID | None) -> PairModel:
    """The latest version of the company's model, or the priors (version 0)."""
    row = conn.execute(
        "SELECT max(version) FROM pair_model_weights WHERE company_id IS NOT DISTINCT FROM %s", (company_id,)
    ).fetchone()
    version = row[0] if row and row[0] is not None else None
    owner = company_id
    if version is None:
        owner, version = None, 0
    rows = conn.execute(
        """
        SELECT feature, level, m_prob, u_prob, pseudo_count, trained_on_decisions
          FROM pair_model_weights WHERE company_id IS NOT DISTINCT FROM %s AND version = %s
        """,
        (owner, version),
    ).fetchall()
    if not rows:
        raise LookupError("Brak wag modelu par (pair_model_weights) - zastosuj migrację 0010")
    m: dict[tuple[str, str], float] = {}
    u: dict[tuple[str, str], float] = {}
    pseudo: dict[tuple[str, str], float] = {}
    thresholds: dict[str, float] = {}
    params: dict[str, float] = {}
    prior = 0.02
    trained = 0
    for feature, level, m_prob, u_prob, pseudo_count, decisions in rows:
        trained = max(trained, decisions)
        if feature == PRIOR:
            prior = float(m_prob)
        elif feature == THRESHOLD:
            thresholds[level] = float(m_prob)
        elif feature == PARAM:
            params[level] = float(m_prob)
        else:
            m[(feature, level)] = float(m_prob)
            u[(feature, level)] = float(u_prob)
            pseudo[(feature, level)] = float(pseudo_count)
    missing = [(f, lv) for f, levels in LEVELS.items() for lv in levels if (f, lv) not in m]
    if missing:
        raise LookupError(f"Niepełne wagi modelu par w wersji {version}: brak {missing}")
    return PairModel(company_id=owner, version=version, m=m, u=u, pseudo=pseudo, prior_match=prior,
                     thresholds=thresholds, params=params, trained_on_decisions=trained)


# ---------------------------------------------------------------------------
# Comparing two records
# ---------------------------------------------------------------------------


@dataclass
class Corpus:
    """Token statistics of the records being compared (rarity of a token)."""

    df: Counter
    size: int
    rare_df_ratio: float
    idf: dict[str, float] = field(default_factory=dict)

    @classmethod
    def of(cls, records: list[DiscoveryRecord], model: PairModel) -> Corpus:
        df, size = document_frequencies(records)
        corpus = cls(df=df, size=max(size, 1), rare_df_ratio=model.params.get("rare_df_ratio", 0.25))
        corpus.idf = {t: math.log((corpus.size + 1) / (n + 0.5)) for t, n in df.items()}
        return corpus

    def weight(self, token: str) -> float:
        return self.idf.get(token, math.log(self.size + 1))

    def is_rare(self, token: str) -> bool:
        return self.df.get(token, 0) <= max(2, self.rare_df_ratio * self.size)


def name_similarity(a: frozenset[str], b: frozenset[str], corpus: Corpus) -> float:
    if not a or not b:
        return 0.0
    shared = sum(corpus.weight(t) for t in a & b)
    return shared / min(sum(corpus.weight(t) for t in a), sum(corpus.weight(t) for t in b))


def compare(a: DiscoveryRecord, b: DiscoveryRecord, corpus: Corpus) -> dict[str, str]:
    """Levels of the features present on both records (missing features are left out)."""
    levels: dict[str, str] = {}
    if a.name_tokens and b.name_tokens:
        sim = name_similarity(a.name_tokens, b.name_tokens, corpus)
        levels["name"] = ("exact" if sim >= NAME_EXACT else "high" if sim >= NAME_HIGH
                          else "partial" if sim >= NAME_PARTIAL else "none")
    if a.client_tokens and b.client_tokens:
        levels["client"] = "agree" if a.client_tokens & b.client_tokens else "disagree"
    if a.window and b.window:
        gap = window_gap_days(a.window, b.window)
        short = window_days(a.window) <= SHORT_WINDOW_DAYS and window_days(b.window) <= SHORT_WINDOW_DAYS
        if gap == 0:
            level = "overlap" if short else "near"
        elif gap <= DATES_NEAR_DAYS:
            level = "near"
        elif gap <= DATES_MID_DAYS:
            level = "mid"
        else:
            level = "far"
        # a booking date says when the money moved, not when the service was: never "far" within a month
        if level == "far" and "booking" in (a.date_source, b.date_source) and gap <= 31:
            level = "mid"
        levels["dates"] = level
    if a.city_key and b.city_key:
        levels["city"] = "agree" if a.city_key == b.city_key else "disagree"
    tokens_a, tokens_b = a.text_tokens | a.name_tokens, b.text_tokens | b.name_tokens
    if tokens_a and tokens_b:
        excluded = a.client_tokens | b.client_tokens | {a.city_key, b.city_key}
        shared = {t for t in tokens_a & tokens_b if t not in excluded}
        levels["rare_token"] = "yes" if any(corpus.is_rare(t) for t in shared) else "no"
    if a.persons and b.persons:
        levels["persons"] = "shared" if a.persons & b.persons else "none"
    return levels


class PairScorer:
    """Cached pair probabilities for one discovery run."""

    def __init__(self, model: PairModel, records: list[DiscoveryRecord]):
        self.model = model
        self.corpus = Corpus.of(records, model)
        self._cache: dict[tuple, tuple[float, dict[str, str]]] = {}

    def explain(self, a: DiscoveryRecord, b: DiscoveryRecord) -> tuple[float, dict[str, str]]:
        key = (a.key, b.key) if a.key <= b.key else (b.key, a.key)
        hit = self._cache.get(key)
        if hit is None:
            levels = compare(a, b, self.corpus)
            hit = (self.model.probability(levels), levels)
            self._cache[key] = hit
        return hit

    def probability(self, a: DiscoveryRecord, b: DiscoveryRecord) -> float:
        return self.explain(a, b)[0]


# ---------------------------------------------------------------------------
# Learning from human decisions
# ---------------------------------------------------------------------------

Key = tuple[str, str]       # (record_type, record_id as text) as stored in decision payloads


def _keys(items: Any) -> list[Key]:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            out.append((item["record_type"], str(item["record_id"])))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            out.append((item[0], str(item[1])))
    return out


def labeled_pairs(conn: psycopg.Connection, company_id: UUID) -> tuple[list[tuple[Key, Key, str]], int]:
    """Pairs of records labeled by the person's cost object decisions (revoked ones left out)."""
    rows = conn.execute(
        """
        SELECT d.id, d.payload FROM l3_decision_log d
         WHERE d.company_id = %s AND d.kind = 'cost_object' AND d.method = 'human'
           AND NOT EXISTS (SELECT 1 FROM l3_decision_log r
                            WHERE r.reverts_decision_id = d.id AND r.kind = 'revoke')
         ORDER BY d.decided_at, d.id
        """,
        (company_id,),
    ).fetchall()
    pairs: list[tuple[Key, Key, str]] = []
    used = 0
    for _, payload in rows:
        action = payload.get("action")
        before = len(pairs)
        if action == "approve":
            anchor = _keys([payload.get("anchor")]) if payload.get("anchor") else []
            members = _keys(payload.get("members"))
            if anchor:
                pairs += [(anchor[0], m, "same") for m in members if m != anchor[0]]
        # "reject" says "this is no cost object", not "these records differ": no pair evidence
        elif action == "split":
            groups = [_keys(g) for g in payload.get("groups") or []]
            for i, group in enumerate(groups):
                if not group:
                    continue
                head = group[0]
                pairs += [(head, m, "same") for m in group[1:]]
                for other in groups[i + 1:]:
                    if other:
                        pairs += [(head, m, "different") for m in other]
                        pairs.append((other[0], head, "different"))
        elif action in ("merge", "distinct"):
            label = "same" if action == "merge" else "different"
            sides = [_keys(s) for s in payload.get("sides") or []]
            for i, side in enumerate(sides):
                for other in sides[i + 1:]:
                    if side and other:
                        pairs += [(side[0], m, label) for m in other]
                        pairs += [(other[0], m, label) for m in side[1:]]
        if len(pairs) > before:
            used += 1
    return pairs, used


def retrain(conn: psycopg.Connection, company_id: UUID, *, created_by: str = "kasa",
            records: list[DiscoveryRecord] | None = None) -> int | None:
    """Count feature levels of labeled pairs and write a new model version (None = nothing new).

    m(level) = (count among "same" pairs + pseudo * prior m) / (pairs with the feature + pseudo),
    u(level) likewise on "different" pairs. Prior, thresholds and parameters are carried over.
    """
    from kasa.core.cost_objects_discovery.records import load_records

    pairs, used = labeled_pairs(conn, company_id)
    priors = load_model(conn, None)
    current = load_model(conn, company_id)
    records = records if records is not None else load_records(conn, company_id)
    by_key = {(r.record_type, str(r.record_id)): r for r in records}
    corpus = Corpus.of(records, priors)

    counts: dict[str, dict[tuple[str, str], int]] = {"same": defaultdict(int), "different": defaultdict(int)}
    totals: dict[str, dict[str, int]] = {"same": defaultdict(int), "different": defaultdict(int)}
    seen: set[tuple] = set()
    pair_counts = {"same": 0, "different": 0}
    for a_key, b_key, label in pairs:
        a, b = by_key.get(a_key), by_key.get(b_key)
        if a is None or b is None or a_key == b_key:
            continue
        ident = (min(a_key, b_key), max(a_key, b_key), label)
        if ident in seen:
            continue
        seen.add(ident)
        pair_counts[label] += 1
        for feature, level in compare(a, b, corpus).items():
            counts[label][(feature, level)] += 1
            totals[label][feature] += 1

    new_m: dict[tuple[str, str], float] = {}
    new_u: dict[tuple[str, str], float] = {}
    for feature, levels in LEVELS.items():
        for level in levels:
            k = (feature, level)
            pseudo = priors.pseudo[k]
            new_m[k] = (counts["same"][k] + pseudo * priors.m[k]) / (totals["same"][feature] + pseudo)
            new_u[k] = (counts["different"][k] + pseudo * priors.u[k]) / (totals["different"][feature] + pseudo)

    def rounded(x: float) -> Decimal:
        return max(Decimal("0.00000001"), Decimal(str(round(x, 8))))

    unchanged = current.version > 0 and all(
        rounded(new_m[k]) == rounded(current.m[k]) and rounded(new_u[k]) == rounded(current.u[k]) for k in new_m
    ) and current.trained_on_decisions == used
    if unchanged or (current.version == 0 and not pair_counts["same"] and not pair_counts["different"]):
        return None
    version = current.version + 1 if current.company_id == company_id else 1
    rows = []
    for (feature, level), m in new_m.items():
        effective = totals["same"][feature] + totals["different"][feature] + priors.pseudo[(feature, level)]
        rows.append((company_id, feature, level, rounded(m), rounded(new_u[(feature, level)]), effective))
    rows.append((company_id, PRIOR, "match", rounded(priors.prior_match), rounded(1 - priors.prior_match), 0))
    rows += [(company_id, THRESHOLD, name, rounded(value), None, 0) for name, value in priors.thresholds.items()]
    rows += [(company_id, PARAM, name, rounded(value), None, 0) for name, value in priors.params.items()]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO pair_model_weights (company_id, feature, level, m_prob, u_prob, pseudo_count, version,
                                            trained_on_decisions, trained_on_pairs, source, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'retrain', %s)
            """,
            [(*row, version, used, Jsonb(pair_counts), created_by) for row in rows],
        )
    return version
