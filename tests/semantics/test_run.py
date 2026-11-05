"""A semantics pass: once per (subject, prompt version, model), cost estimate without calls, new versions."""

from __future__ import annotations

from decimal import Decimal

from kasa.core.semantics import prompts
from kasa.core.semantics.run import run_semantics

from .helpers import CountingMock


def _counts(conn, company_id) -> tuple[int, int, int, int]:
    return conn.execute(
        """
        SELECT (SELECT count(*) FROM llm_call_log WHERE company_id = %(c)s),
               (SELECT count(*) FROM llm_call_log WHERE company_id = %(c)s AND status = 'ok'),
               (SELECT count(*) FROM l1_semantics WHERE company_id = %(c)s),
               (SELECT count(*) FROM pseudonym_key WHERE company_id = %(c)s)
        """,
        {"c": company_id},
    ).fetchone()


def test_second_pass_makes_no_call(conn, loaded):
    mock = CountingMock()
    first = run_semantics(conn, loaded, provider=mock)
    assert first.calls == mock.calls == 82 + 50 + 20
    assert first.blocked == {"sensitive": 1}
    before = _counts(conn, loaded.id)
    second = run_semantics(conn, loaded, provider=mock)
    assert second.calls == 0 and mock.calls == 152 and not second.blocked
    assert all(c.already_read == c.subjects for c in second.by_scope.values())
    assert _counts(conn, loaded.id) == before                     # not even a blocked attempt is logged again


def test_estimate_calls_nothing_and_writes_nothing(conn, loaded, monkeypatch):
    monkeypatch.setenv("KASA_LLM_PRICE_PER_1K", "0,002")
    mock = CountingMock()
    before = _counts(conn, loaded.id)
    estimate = run_semantics(conn, loaded, dry_run=True, provider=mock)
    assert mock.calls == 0 and _counts(conn, loaded.id) == before
    assert sum(c.to_call for c in estimate.by_scope.values()) == 152 and estimate.blocked == {"sensitive": 1}
    assert estimate.chars > 100_000
    assert estimate.cost_estimate == (Decimal(estimate.chars) * Decimal("0.002") / 1000).quantize(Decimal("0.000001"))
    assert "Szacunek" in estimate.summary() and str(estimate.cost_estimate) in estimate.summary()
    # the real pass sends about as much as estimated
    real = run_semantics(conn, loaded, provider=mock)
    assert 0.8 * estimate.chars < real.chars < 1.2 * estimate.chars


def test_a_new_prompt_version_reads_again_and_supersedes(conn, loaded, monkeypatch):
    mock = CountingMock()
    run_semantics(conn, loaded, "transactions", provider=mock)
    monkeypatch.setitem(prompts.PROMPTS, "title_intent", "v2")
    again = run_semantics(conn, loaded, "transactions", provider=mock)
    assert again.calls == 82
    rows = conn.execute(
        """
        SELECT prompt_version, superseded_by IS NULL, count(*) FROM l1_semantics
         WHERE company_id = %s AND subject_type = 'transaction' GROUP BY 1, 2 ORDER BY 1, 2
        """,
        (loaded.id,),
    ).fetchall()
    assert rows == [("v1", False, 82), ("v2", True, 82)]
    assert conn.execute("SELECT count(*) FROM title_semantics_current WHERE company_id = %s",
                        (loaded.id,)).fetchone() == (82,)
