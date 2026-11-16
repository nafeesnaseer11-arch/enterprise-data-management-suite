"""End-to-end tests in local mode (no cloud or API credentials needed)."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pandas as pd
import pytest

from edms.config import load_settings
from edms.etl.profiler import detect_pii, infer_type, normalize_name
from edms.governance import store
from edms.governance.policies import AccessDenied, PolicyEngine
from edms.suite import Suite


@pytest.fixture(scope="module")
def suite(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("edms")
    cfg = load_settings()
    cfg = copy.deepcopy(cfg)
    cfg["mode"] = "local"
    cfg["rag"]["llm"]["provider"] = "offline"
    cfg["local"].update({"aws_s3_root": str(tmp / "s3"), "aws_rds_root": str(tmp / "rds"),
                         "gcs_landing_root": str(tmp / "gcs"), "warehouse_path": str(tmp / "wh.duckdb"),
                         "vector_store_path": str(tmp / "vec")})
    cfg["aws"]["rds_databases"][0]["url"] = f"sqlite:///{tmp / 'rds' / 'acme_hr.db'}"
    s = Suite(cfg)
    s.run_end_to_end(on_event=lambda **e: None)
    return s


def test_profiler_units():
    assert normalize_name("Customer ID") == "customer_id"
    assert normalize_name("signupDate") == "signup_date"
    assert infer_type(pd.Series(["$1,200.50", "30.00"]), 0.95) == ("FLOAT64", True)
    assert infer_type(pd.Series(["2025-01-01", "2026-02-03"]), 0.95)[0] == "DATE"
    assert infer_type(pd.Series(["00123", "00456"]), 0.95)[0] == "STRING"  # leading zeros kept
    assert detect_pii("contact", pd.Series(["a@b.com", "c@d.org"])) == "EMAIL"
    assert detect_pii("x", pd.Series(["123-45-6789", "987-65-4321"])) == "GOV_ID"


def test_all_layers_built(suite):
    wh = suite.wh
    assert set(wh.list_tables("bronze")) >= {"customers", "orders", "products", "support_tickets", "invoices",
                                             "employees", "departments"}
    assert {"customers", "orders"} <= set(wh.list_tables("silver"))
    assert len(wh.list_tables("gold")) == len(suite.cfg["gold_models"])


def test_reconciliation_and_dedup(suite):
    rec = store.read(suite.wh, "reconciliation").set_index("table_name")
    assert (rec["source_rows"] == rec["bronze_rows"]).all()
    assert rec.loc["orders", "duplicates_removed"] == 120
    assert rec.loc["customers", "quarantined_rows"] == 6
    # silver orders: currency strings typed to FLOAT64, NA region preserved (not treated as null)
    schema = dict(suite.wh.table_schema("silver", "orders"))
    assert schema["unit_price"] == "FLOAT64" and schema["order_date"] == "DATE"
    assert suite.wh.query("SELECT COUNT(*) n FROM edms_silver.orders WHERE region IS NULL")["n"][0] == 0


def test_classification_and_propagation(suite):
    cols = store.read(suite.wh, "catalog_columns")
    get = lambda l, t, c: cols[(cols.layer == l) & (cols.table_name == t) & (cols.column_name == c)].iloc[0]
    assert get("silver", "employees", "ssn")["classification"] == "RESTRICTED"
    assert get("silver", "invoices", "card_number")["classification"] == "RESTRICTED"
    assert get("gold", "customer_360", "email_address")["classification"] == "CONFIDENTIAL"  # propagated
    assert get("gold", "headcount_by_department", "avg_salary")["classification"] == "CONFIDENTIAL"


def test_policies(suite):
    cat, cols = store.read(suite.wh, "catalog_tables"), store.read(suite.wh, "catalog_columns")
    p = suite.policy
    df, info = p.governed_query(suite.wh, "business_analyst",
                                "SELECT first_name, lifetime_value FROM gold.customer_360 LIMIT 5", cat, cols)
    assert df["first_name"].str.startswith("tok_").all()
    assert df["lifetime_value"].notna().all()
    with pytest.raises(AccessDenied):
        p.governed_query(suite.wh, "business_analyst", "SELECT * FROM silver.employees", cat, cols)
    with pytest.raises(AccessDenied):
        p.governed_query(suite.wh, "data_admin", "DELETE FROM gold.customer_360", cat, cols)
    with pytest.raises(AccessDenied):
        p.governed_query(suite.wh, "data_admin", "SELECT 1; DROP TABLE gold.customer_360", cat, cols)
    hr, _ = p.governed_query(suite.wh, "hr_partner", "SELECT ssn, salary FROM silver.employees LIMIT 3", cat, cols)
    assert (hr["ssn"] == "***REDACTED***").all() and hr["salary"].notna().all()
    audit = store.read(suite.wh, "audit_log")
    assert (audit["status"] == "DENIED").sum() >= 3


def test_vector_store_has_no_pii(suite):
    docs = suite.vstore.docs
    assert docs and not any("@example.com" in d["text"] for d in docs if d["meta"]["kind"] == "row")


def test_retrieval_respects_role(suite):
    hits = suite.chatbot.retrieve("employee salary by department headcount", "business_analyst", k=10)
    assert all(not (h["meta"].get("fq") or "").startswith(("silver.employees", "gold.headcount")) for h in hits)
    hits = suite.chatbot.retrieve("headcount by department", "data_admin", k=3)
    assert any(h["meta"].get("fq") == "gold.headcount_by_department" for h in hits)


def test_offline_chat(suite):
    r = suite.chatbot.ask("Which region had the highest revenue?", "executive")
    assert r["mode"] == "offline" and r["queries"] and "result" in r["queries"][0]


def test_claude_tool_loop_with_mock(suite, monkeypatch):
    """Exercises the tool-use loop with a fake Anthropic client (no network)."""
    calls = []

    def create(**kw):
        calls.append({**kw, "messages": list(kw["messages"])})  # snapshot per call
        if len(calls) == 1:
            return SimpleNamespace(stop_reason="tool_use", model="claude-opus-5-5", content=[
                SimpleNamespace(type="tool_use", id="tu_1", name="run_sql",
                                input={"sql": "SELECT region, SUM(revenue) AS revenue FROM gold.revenue_by_month "
                                              "GROUP BY region ORDER BY revenue DESC", "purpose": "rank regions"})])
        return SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5",
                               content=[SimpleNamespace(type="text", text="EMEA leads revenue [1].")])

    bot = suite.chatbot
    monkeypatch.setattr(type(bot), "llm_enabled", property(lambda self: True))
    bot._client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    r = bot.ask("Which region had the highest revenue?", "executive")
    assert r["answer"] == "EMEA leads revenue [1]." and r["mode"] == "llm:claude-opus-5-5"
    assert calls[0]["model"] == "claude-opus-5-5" and calls[0]["fallbacks"] == "default"
    tool_result = calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and not tool_result["is_error"]
    assert r["queries"][0]["result"].shape[0] == 4
    bot._client = None
