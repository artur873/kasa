"""The registry of metrics: one definition per (key, version), the SHA-256 of the code recorded in the database
and in the lock file - a change of a definition without a new version is detected."""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from kasa.core.metrics import registry
from kasa.core.metrics.base import DefinitionChanged, MetricError, UnknownMetric, parse_period

STARTING_METRICS = {  # briefing, step 8 (+ payables, balances, data state, queue state, company result, cash flow)
    "revenue_accrual_net", "revenue_cash_gross", "receivables_open", "dso_90", "margin_accrual",
    "margin_fully_loaded", "cash_contribution", "costs_without_document", "allocation_coverage", "payables_open",
    "account_balances", "data_state", "queue_state", "company_result", "cash_flow",
}


def test_every_starting_metric_has_a_versioned_definition_with_its_traps():
    assert set(registry.keys()) == STARTING_METRICS
    for key in registry.keys():
        cls = registry.latest(key)
        info = registry.describe(cls)
        assert info["version"] >= 1 and info["ref"] == f"{key}@{info['version']}"
        assert info["name_pl"] and info["description_pl"] and info["implementation"].startswith("python:")
        assert info["grain"] in ("company", "cost_object", "counterparty", "document", "day")
        assert info["unit"] in ("pln_minor", "days", "ratio", "count")
    for key in ("revenue_accrual_net", "receivables_open", "margin_accrual", "cash_contribution",
                "costs_without_document", "allocation_coverage", "dso_90", "revenue_cash_gross"):
        assert registry.latest(key).traps_pl, key
    assert any("Zaliczka" in t for t in registry.latest("revenue_accrual_net").traps_pl)
    assert any("od daty wystawienia" in t for t in registry.latest("receivables_open").traps_pl)
    assert any("UNASSIGNED" in t for t in registry.latest("margin_accrual").traps_pl)
    assert any("margin_fully_loaded" in t for t in registry.latest("margin_accrual").traps_pl)


def test_the_lock_file_records_the_code_of_every_definition():
    """A change of the code of a definition without a new version fails here: add version+1 instead."""
    lock = registry.read_lock()
    current = registry.lock_entries()
    missing = sorted(set(current) - set(lock))
    assert not missing, f"nowe wersje bez wpisu w definitions.lock.json: {missing} (python -m kasa metrics lock)"
    changed = sorted(ref for ref, digest in current.items() if lock[ref] != digest)
    assert not changed, f"kod definicji zmieniony bez nowej wersji: {changed}"


def test_write_lock_refuses_to_hide_a_changed_definition(monkeypatch):
    ref = registry.latest("receivables_open").ref()
    monkeypatch.setattr(registry, "read_lock", lambda: {ref: "0" * 64})
    with pytest.raises(DefinitionChanged, match="bez nowej wersji"):
        registry.write_lock()


def test_sync_writes_definitions_once_and_detects_a_change_without_a_new_version(conn, make, monkeypatch):
    report = registry.sync_definitions(conn)
    versions = len(registry.implementations())
    assert len(report["added"]) + len(report["unchanged"]) == versions >= len(STARTING_METRICS)
    again = registry.sync_definitions(conn)
    assert again["added"] == [] and len(again["unchanged"]) == versions
    row = conn.execute("SELECT code_sha256, traps_pl, grain FROM metrics.metric_definition WHERE key = "
                       "'receivables_open' AND version = 1").fetchone()
    assert row[0] == registry.code_hash(registry.get("receivables_open", 1)) and row[1] and row[2] == "document"
    # the database refuses to change a definition in place
    with pytest.raises(psycopg.Error) as err, conn.transaction():
        conn.execute("UPDATE metrics.metric_definition SET description_pl = 'inna' WHERE key = 'dso_90'")
    assert err.value.sqlstate == "KS013"
    # the code changed, the version did not: sync and every answer refuse
    real = registry.code_hash
    monkeypatch.setattr(registry, "code_hash",
                        lambda cls: "f" * 64 if cls.key == "dso_90" else real(cls))
    with pytest.raises(DefinitionChanged, match="dso_90@1"):
        registry.sync_definitions(conn, refresh_views=False)
    company = make.company()
    with pytest.raises(DefinitionChanged, match="nowej wersji"):
        registry.compute(conn, company, "dso_90")
    # deprecating is the only change allowed
    conn.execute("UPDATE metrics.metric_definition SET status = 'deprecated' WHERE key = 'queue_state'")
    result = registry.compute(conn, company, "queue_state")
    assert any("wycofana" in w for w in result.warnings)


def test_unknown_metric_dimension_filter_and_period(conn, make):
    registry.sync_definitions(conn, refresh_views=False)
    company = make.company()
    with pytest.raises(UnknownMetric):
        registry.compute(conn, company, "cit_2026")
    with pytest.raises(MetricError, match="wymiar"):
        registry.compute(conn, company, "receivables_open", dimensions=["osoba"])
    with pytest.raises(MetricError, match="filtr"):
        registry.compute(conn, company, "margin_accrual", filters={"formula": "x*2"})
    with pytest.raises(MetricError):
        registry.compute(conn, company, "company_result", period="październik")
    assert parse_period("2026-10").months == ["2026-10"]
    year = parse_period("2026", as_of=date(2026, 10, 31))
    assert (year.start, year.end) == (date(2026, 1, 1), date(2026, 10, 31)) and len(year.months) == 10
    assert parse_period("2026-10-05..2026-11-02").months == ["2026-10", "2026-11"]


def test_every_answer_carries_the_metadata_of_the_briefing(mconn, modm):
    for key in registry.keys():
        version = registry.latest(key).version
        result = registry.compute(mconn, modm.id, key).as_dict()
        assert result["metric"]["ref"] == f"{key}@{version}" and len(result["metric"]["code_sha256"]) == 64
        assert result["data_state"]["as_of"] == "2026-10-31", key
        assert 0 <= result["unconfirmed_share"] <= 1 and result["unconfirmed_share_pl"].endswith("%")
        assert "decisions" in result["provenance"] and "suggestions" in result["provenance"]
        assert result["metrics_used"][0] == {"key": key, "version": version}
        assert isinstance(result["warnings"], list) and isinstance(result["traps_pl"], list)


def test_a_new_version_lives_next_to_the_old_one(conn, make):
    """margin_accrual@3 (no number while the object waits for its card) next to @1 and @2, both deprecated."""
    report = registry.sync_definitions(conn, refresh_views=False)
    assert "margin_accrual@1" in report["deprecated"] or conn.execute(
        "SELECT status FROM metrics.metric_definition WHERE key = 'margin_accrual' AND version = 1").fetchone() == (
        "deprecated",)
    assert registry.latest("margin_accrual").version == 3
    assert len({registry.code_hash(registry.get("margin_accrual", v)) for v in (1, 2, 3)}) == 3
    company = make.company()
    old = registry.compute(conn, company, "margin_accrual", version=1, filters={"cost_object": "Nie ma"})
    assert old.value == 0 and any("wycofana" in w for w in old.warnings)
    with pytest.raises(MetricError, match="nie pasuje"):
        registry.compute(conn, company, "margin_accrual", filters={"cost_object": "Nie ma"})
