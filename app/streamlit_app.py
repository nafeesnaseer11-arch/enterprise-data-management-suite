"""Enterprise Data Management Suite - unified UI.

    streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import plotly.express as px
import streamlit as st

from edms.config import LAYERS
from edms.etl import transform
from edms.governance import lineage as lin
from edms.governance import store
from edms.governance.catalog import load_profiles
from edms.governance.policies import AccessDenied, bigquery_governance_script
from edms.suite import Suite

st.set_page_config(page_title="Enterprise Data Management Suite", page_icon="🧭", layout="wide")

st.markdown("""
<style>
:root { --edms-accent:#4F46E5; --edms-card:rgba(127,127,127,.08); --edms-border:rgba(127,127,127,.25); }
.block-container { padding-top: 1.6rem; }
.edms-hero { padding: 1.1rem 1.4rem; border-radius: 14px; margin-bottom: 1rem; color: #fff;
  background: linear-gradient(110deg, #1E1B4B 0%, #4F46E5 55%, #0EA5E9 100%); }
.edms-hero h1 { color:#fff; font-size: 1.7rem; margin: 0 0 .2rem 0; padding:0; }
.edms-hero p { margin: 0; opacity: .9; }
.edms-pill { display:inline-block; padding:.1rem .6rem; border-radius:999px; font-size:.75rem; font-weight:600;
  background: var(--edms-card); border:1px solid var(--edms-border); margin-right:.3rem; }
.edms-mod { border:1px solid var(--edms-border); background: var(--edms-card); border-radius: 12px; padding: .9rem 1rem; height: 100%; }
.edms-mod h4 { margin: 0 0 .3rem 0; }
.edms-mod p { margin: 0; font-size: .9rem; opacity: .85; }
</style>""", unsafe_allow_html=True)

CLASS_COLORS = {"PUBLIC": "#16A34A", "INTERNAL": "#2563EB", "CONFIDENTIAL": "#D97706", "RESTRICTED": "#DC2626"}


@st.cache_resource(show_spinner="Starting the suite…")
def get_suite() -> Suite:
    s = Suite()
    # Cloud Run demo: the container starts empty, so build the sample estate once per instance
    if os.getenv("EDMS_AUTO_BOOTSTRAP") == "1" and s.cfg["mode"] == "local" and s.status()["gold_tables"] == 0:
        s.run_end_to_end(on_event=lambda **e: print(f"[bootstrap:{e['stage']}] {e['message']}"), actor="system")
    return s


suite = get_suite()
wh, policy = suite.wh, suite.policy


def gov(table: str) -> pd.DataFrame:
    return store.read(wh, table)


def hero(title: str, subtitle: str):
    st.markdown(f'<div class="edms-hero"><h1>{title}</h1><p>{subtitle}</p></div>', unsafe_allow_html=True)


def run_with_log(label: str, fn):
    """Run fn(on_event) inside a status box with a live log and progress bar."""
    lines: list[str] = []
    with st.status(label, expanded=True) as box:
        prog, log = st.progress(0.0), st.empty()

        def on_event(stage, message, level="info", progress=None):
            icon = {"warning": "⚠️", "error": "❌"}.get(level, "•")
            lines.append(f"{icon} [{stage}] {message}")
            log.code("\n".join(lines[-40:]), language=None)
            if progress is not None:
                prog.progress(min(float(progress), 1.0))
        try:
            result = fn(on_event)
            prog.progress(1.0)
            box.update(label=f"{label} - done", state="complete", expanded=False)
            return result
        except Exception as e:
            box.update(label=f"{label} - failed: {e}", state="error", expanded=True)
            st.exception(e)
            return None


# ============================================================== sidebar
with st.sidebar:
    st.markdown("### 🧭 Enterprise Data\n### Management Suite")
    st.caption("Intelligent ETL · Meta RAG · Data Governance")
    page = st.radio("Navigate", ["🏠 Overview", "⚙️ Intelligent ETL", "💬 Meta RAG", "🛡️ Data Governance"],
                    label_visibility="collapsed")
    st.divider()
    role = st.selectbox("Acting as role", list(policy.roles), index=0,
                        help="Every query, chatbot answer and preview is governed by this role's policy.")
    r = policy.role(role)
    st.caption(f"{r['description']}\n\n**Layers:** {', '.join(r['layers'])}  \n**Domains:** {', '.join(r['domains'])}  \n"
               f"**Clearance:** {r['max_classification']}")
    st.divider()
    stt = suite.status()
    st.markdown(f'<span class="edms-pill">mode: {stt["mode"]}</span><span class="edms-pill">{stt["dialect"]}</span>'
                f'<span class="edms-pill">LLM: {"Claude" if stt["llm"] else "offline"}</span>', unsafe_allow_html=True)


# ============================================================== overview
def page_overview():
    hero("Enterprise Data Management Suite",
         "Migrate the organisation's AWS estate to Google Cloud, refine it through a medallion architecture in "
         "BigQuery, govern it, and ask it questions in plain language.")
    stt = suite.status()
    c = st.columns(6)
    c[0].metric("🥉 Bronze tables", stt["bronze_tables"], f"{stt['bronze_rows']:,} rows", delta_color="off")
    c[1].metric("🥈 Silver tables", stt["silver_tables"], f"{stt['silver_rows']:,} rows", delta_color="off")
    c[2].metric("🥇 Gold tables", stt["gold_tables"], f"{stt['gold_rows']:,} rows", delta_color="off")
    lr = stt["last_run"]
    c[3].metric("✅ DQ score", f"{lr['dq_score']}%" if lr else "–", lr["status"] if lr else "no runs", delta_color="off")
    c[4].metric("🧠 RAG documents", f"{stt['documents']:,}", stt["embedder"], delta_color="off")
    c[5].metric("🤖 LLM", "Claude" if stt["llm"] else "Offline", suite.cfg["rag"]["llm"]["model"] if stt["llm"] else
                "set ANTHROPIC_API_KEY", delta_color="off")

    st.write("")
    m = st.columns(3)
    m[0].markdown('<div class="edms-mod"><h4>⚙️ Intelligent ETL</h4><p>Auto-discovers every S3 prefix and RDS table, '
                  'lands raw copies in GCS, infers types, keys and PII, then builds Bronze → Silver → Gold in BigQuery '
                  'with reconciliation and schema-drift detection.</p></div>', unsafe_allow_html=True)
    m[1].markdown('<div class="edms-mod"><h4>💬 Meta RAG</h4><p>Embeds catalog metadata, glossary and curated data into '
                  'a vector database. Claude answers questions, writes governed SQL and cites its sources.</p></div>',
                  unsafe_allow_html=True)
    m[2].markdown('<div class="edms-mod"><h4>🛡️ Data Governance</h4><p>Catalog, lineage, classification, role-based '
                  'access with masking, data-quality scorecards, retention and a full audit trail - exported as native '
                  'BigQuery policies.</p></div>', unsafe_allow_html=True)

    st.subheader("Architecture")
    st.graphviz_chart("""
digraph A { rankdir=LR; bgcolor="transparent"; compound=true;
 node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11, fontcolor="#111111"];
 edge [color="#888888"];
 subgraph cluster_aws { label="AWS (source)"; fontcolor="#FF9900"; color="#FF9900"; style="rounded";
   s3 [label="S3 buckets\\nCSV · JSON · Parquet", fillcolor="#FFE0B2"]; rds [label="RDS databases", fillcolor="#FFE0B2"]; }
 subgraph cluster_gcp { label="Google Cloud (target)"; fontcolor="#4285F4"; color="#4285F4"; style="rounded";
   etl [label="Intelligent ETL\\ndiscover · profile · PII · drift", fillcolor="#C7D2FE"];
   gcs [label="GCS landing\\n(raw, immutable)", fillcolor="#BBDEFB"];
   subgraph cluster_bq { label="BigQuery - medallion"; color="#888888"; style="rounded,dashed";
     bronze [label="Bronze\\nraw as text", fillcolor="#E6B98A"]; silver [label="Silver\\ntyped · clean · dedup", fillcolor="#D9D9DD"];
     gold [label="Gold\\nbusiness models", fillcolor="#F3E3A0"]; }
   gov [label="Data Governance\\ncatalog · lineage · policies\\nDQ · audit · retention", fillcolor="#BBF7D0"];
   emb [label="Embeddings\\n(Vertex AI / local)", fillcolor="#E9D5FF"]; vdb [label="Vector DB\\n(BigQuery VECTOR_SEARCH)", fillcolor="#E9D5FF"];
   llm [label="Claude chatbot\\n+ governed SQL tool", fillcolor="#FBCFE8"]; }
 user [label="Business users\\n(role-based)", shape=ellipse, fillcolor="#F1F5F9"];
 s3 -> etl; rds -> etl; etl -> gcs -> bronze -> silver -> gold;
 gold -> emb; gov -> emb [label="metadata", fontsize=9]; emb -> vdb -> llm; llm -> gold [label="SQL", fontsize=9, style=dashed];
 bronze -> gov [style=dotted, arrowhead=none]; silver -> gov [style=dotted, arrowhead=none]; gold -> gov [style=dotted, arrowhead=none];
 user -> llm;
}""")

    st.subheader("Run everything")
    st.caption("Discovery → AWS-to-GCS landing → Bronze → Silver → Gold → data quality → catalog & lineage → RAG index")
    if st.button("▶ Run end-to-end", type="primary"):
        res = run_with_log("End-to-end migration", lambda ev: suite.run_end_to_end(ev, actor=role))
        if res:
            st.success(f"Run {res[0].run_id}: {res[0].status} · DQ {res[0].dq_score}% · {res[1]:,} documents indexed")
            st.rerun()

    rec = gov("reconciliation")
    if not rec.empty:
        st.subheader("Latest migration reconciliation")
        st.dataframe(rec[rec["run_id"] == rec["run_id"].max()].drop(columns=["run_id"]), hide_index=True, width="stretch")


# ============================================================== ETL
def page_etl():
    hero("⚙️ Intelligent ETL", "AWS → GCS landing → BigQuery Bronze → Silver → Gold, driven entirely by metadata.")
    tabs = st.tabs(["🔎 Discovery", "▶️ Run pipeline", "🗂️ Layer explorer", "⚖️ Reconciliation", "🕓 Runs & drift"])

    with tabs[0]:
        st.caption("Every prefix in each configured S3 bucket and every table in each RDS database becomes a dataset.")
        if st.button("Scan AWS estate"):
            st.session_state["discovered"] = suite.discover()
        disc = st.session_state.get("discovered") or (gov("source_inventory").to_dict("records"))
        if disc:
            df = pd.DataFrame(disc)
            c = st.columns(4)
            c[0].metric("Datasets", len(df))
            c[1].metric("Objects / tables", int(df["objects"].sum()))
            c[2].metric("Size (KB)", f"{df['size_kb'].sum():,.0f}")
            c[3].metric("Domains", df["domain"].nunique())
            st.dataframe(df, hide_index=True, width="stretch")
        elif suite.cfg["mode"] == "local":
            st.info("No AWS data found yet.")
            if st.button("Generate sample organisation data"):
                suite.generate_sample_data()
                st.rerun()

    with tabs[1]:
        layers = st.multiselect("Layers to build", list(LAYERS), default=list(LAYERS))
        also_index = st.checkbox("Refresh the Meta RAG index afterwards", value=True)
        if st.button("▶ Run pipeline", type="primary", disabled=not layers):
            def go(ev):
                run = suite.run_pipeline(tuple(l for l in LAYERS if l in layers), ev, actor=role)
                if also_index:
                    suite.build_index(ev)
                return run
            run = run_with_log("Intelligent ETL", go)
            if run:
                st.success(f"{run.run_id}: {run.status} · DQ score {run.dq_score}%")
                if run.errors:
                    st.warning("\n".join(run.errors))

    with tabs[2]:
        c1, c2 = st.columns([1, 3])
        layer = c1.radio("Layer", list(LAYERS), format_func=lambda l: {"bronze": "🥉 Bronze", "silver": "🥈 Silver", "gold": "🥇 Gold"}[l])
        tables = wh.list_tables(layer)
        if not tables:
            c2.info("No tables yet - run the pipeline.")
        else:
            t = c2.selectbox("Table", tables)
            cat = gov("catalog_tables")
            row = cat[cat["fq_name"] == f"{layer}.{t}"]
            if not row.empty:
                c2.caption(row.iloc[0]["description"])
            try:
                df, info = policy.governed_query(wh, role, f"SELECT * FROM {layer}.{t}", cat, gov("catalog_columns"), limit=100)
                if info["masked_columns"]:
                    c2.caption(f"🔒 Masked for role **{role}**: {', '.join(info['masked_columns'])}")
                c2.dataframe(df, hide_index=True, width="stretch")
            except AccessDenied as e:
                c2.error(f"🚫 {e}")
            prof = load_profiles(wh).get(t.removeprefix("quarantine__"))
            if prof and layer in ("bronze", "silver"):
                with st.expander("🧠 Intelligent profile (types, keys, PII inferred from data)"):
                    st.dataframe(pd.DataFrame(prof.to_records())[["name", "inferred_type", "strip_currency", "null_ratio",
                                 "distinct_ratio", "pii_type", "classification", "sample_values", "description"]],
                                 hide_index=True, width="stretch")
                    st.caption(f"Detected primary key: **{prof.primary_key}**")
                if layer == "silver" and not t.startswith("quarantine__"):
                    with st.expander("🛠️ Generated Silver SQL (runs inside the warehouse)"):
                        st.code(transform.silver_sql(wh, prof, wh.fq("bronze", t)), language="sql")
            if layer == "gold":
                model = next((m for m in suite.cfg["gold_models"] if m["name"] == t), None)
                if model:
                    with st.expander("🛠️ Gold model SQL"):
                        st.code(wh.render(model["sql"]), language="sql")

    with tabs[3]:
        rec = gov("reconciliation")
        if rec.empty:
            st.info("No runs yet.")
        else:
            run_id = st.selectbox("Run", sorted(rec["run_id"].unique(), reverse=True))
            sub = rec[rec["run_id"] == run_id].drop(columns=["run_id"])
            if "source_rows" in sub:
                sub["source_vs_bronze"] = (sub["source_rows"] == sub["bronze_rows"]).map({True: "✅ match", False: "❌ mismatch"})
            st.dataframe(sub, hide_index=True, width="stretch")
            long = sub.melt(id_vars="table_name", value_vars=[c for c in ["source_rows", "bronze_rows", "silver_rows",
                            "duplicates_removed", "quarantined_rows"] if c in sub], var_name="stage", value_name="rows")
            st.plotly_chart(px.bar(long, x="table_name", y="rows", color="stage", barmode="group", height=360), width="stretch")

    with tabs[4]:
        runs = gov("pipeline_runs")
        st.dataframe(runs.sort_values("started_at", ascending=False) if not runs.empty else runs, hide_index=True, width="stretch")
        st.markdown("**Schema drift events**")
        drift = gov("schema_drift")
        if drift.empty:
            st.caption("No drift detected.")
        else:
            st.dataframe(drift, hide_index=True, width="stretch")


# ============================================================== RAG
SUGGESTIONS = ["Which region had the highest revenue in 2026?", "Who are our top 5 customers by lifetime value?",
               "What data do we have about employees and who owns it?", "Which support category takes longest to resolve?",
               "How much invoiced revenue is still outstanding by payment method?", "What does lifetime value mean?"]


def page_rag():
    hero("💬 Meta RAG", "Ask questions about the organisation's data. Answers are grounded in the catalog, glossary "
                        "and governed SQL - and respect your role.")
    c = st.columns([2, 2, 2, 1.4])
    c[0].metric("LLM", suite.cfg["rag"]["llm"]["model"] if suite.chatbot.llm_enabled else "Offline mode")
    c[1].metric("Embedder / vector DB", f"{suite.embedder.name} / {type(suite.vstore).__name__.replace('VectorStore', '')}")
    c[2].metric("Indexed documents", f"{suite.vstore.count():,}")
    if c[3].button("🔄 Rebuild index"):
        run_with_log("Building Meta RAG index", suite.build_index)
        st.rerun()
    if not suite.chatbot.llm_enabled:
        st.info("Claude is not configured, so answers use retrieval + a heuristic query. "
                "Export `ANTHROPIC_API_KEY` and restart to enable full LLM answers with SQL reasoning.")
    with st.expander("Index contents"):
        stats = suite.vstore.stats()
        if stats.empty:
            st.caption("Empty - run the pipeline first.")
        else:
            st.dataframe(stats, hide_index=True)

    chat = st.session_state.setdefault("chat", [])
    if not chat:
        st.caption("Try one of these:")
        cols = st.columns(3)
        for i, q in enumerate(SUGGESTIONS):
            if cols[i % 3].button(q, key=f"sugg{i}", width="stretch"):
                st.session_state["pending"] = q
                st.rerun()
    else:
        if st.button("🧹 Clear conversation"):
            st.session_state["chat"] = []
            st.rerun()

    for msg in chat:
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.markdown(f"{msg['content']}  \n<span class='edms-pill'>as {msg['as_role']}</span>", unsafe_allow_html=True)
            else:
                render_answer(msg["result"])

    q = st.chat_input("Ask about your data…") or st.session_state.pop("pending", None)
    if q:
        chat.append({"role": "user", "content": q, "as_role": role})
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            with st.spinner("Retrieving context and reasoning…"):
                history = [{"role": m["role"], "content": m["content"] if m["role"] == "user" else m["result"]["answer"]}
                           for m in chat[:-1]]
                result = suite.chatbot.ask(q, role, history)
            render_answer(result)
        chat.append({"role": "assistant", "content": result["answer"], "result": result})


def render_answer(res: dict):
    st.markdown(res["answer"])
    st.caption(f"mode: {res['mode']} · role: {res['role']}")
    for i, qy in enumerate(res["queries"]):
        with st.expander(f"🧮 SQL {i + 1}" + (" - blocked/failed" if "error" in qy else f" - {qy['info']['rows']} rows"),
                         expanded=res["mode"] == "offline"):
            st.code(qy["sql"], language="sql")
            if "error" in qy:
                st.error(qy["error"])
            else:
                if qy["info"]["masked_columns"]:
                    st.caption(f"🔒 masked: {', '.join(qy['info']['masked_columns'])}")
                st.dataframe(qy["result"], hide_index=True, width="stretch")
    srcs = res["sources"] + res.get("extra_sources", [])
    if srcs:
        with st.expander(f"📚 Sources ({len(srcs)})"):
            for i, d in enumerate(srcs, 1):
                st.markdown(f"**[{i}] {d['id']}** · score {d['score']:.2f}")
                st.text(d["text"][:700])


# ============================================================== governance
def badge(cls: str) -> str:
    return f'<span style="color:{CLASS_COLORS.get(cls, "#666")};font-weight:700">● {cls}</span>'


def page_governance():
    hero("🛡️ Data Governance", "Catalog · lineage · classification · access policies · data quality · audit")
    cat, cols = gov("catalog_tables"), gov("catalog_columns")
    if cat.empty:
        st.info("The catalog is empty - run the pipeline first.")
        return
    tabs = st.tabs(["📚 Catalog", "🧬 Lineage", "🔐 Access & policies", "🕵️ Sensitive data", "✅ Data quality", "📜 Audit log"])

    with tabs[0]:
        f = st.columns([3, 1, 1, 1])
        text = f[0].text_input("Search tables, columns, descriptions", placeholder="e.g. email, revenue, hr")
        lay = f[1].multiselect("Layer", list(LAYERS))
        dom = f[2].multiselect("Domain", sorted(cat["domain"].unique()))
        cls = f[3].multiselect("Classification", list(CLASS_COLORS))
        view = cat.copy()
        if text:
            hit_cols = cols[cols.apply(lambda r: text.lower() in f"{r.column_name} {r.description}".lower(), axis=1)]
            hit = set(hit_cols["layer"] + "." + hit_cols["table_name"])
            view = view[view.apply(lambda r: text.lower() in f"{r.fq_name} {r.description} {r.domain}".lower()
                                   or r.fq_name in hit, axis=1)]
        if lay: view = view[view["layer"].isin(lay)]
        if dom: view = view[view["domain"].isin(dom)]
        if cls: view = view[view["classification"].isin(cls)]
        st.dataframe(view[["fq_name", "domain", "owner", "classification", "row_count", "column_count", "dq_score",
                           "source_system", "retention_days", "description"]], hide_index=True, width="stretch")
        pick = st.selectbox("Table details", view["fq_name"].tolist())
        if pick:
            t = cat[cat["fq_name"] == pick].iloc[0]
            st.markdown(f"#### `{pick}` {badge(t['classification'])}", unsafe_allow_html=True)
            st.markdown(f"{t['description']}  \n**Owner:** {t['owner']} · **Domain:** {t['domain']} · "
                        f"**Physical:** `{t['physical_name']}` · **Source:** {t['source_location']} · "
                        f"**Retention:** {t['retention_days']} days")
            cc = cols[(cols["layer"] + "." + cols["table_name"]) == pick]
            st.dataframe(cc[["column_name", "data_type", "classification", "pii_type", "description"]], hide_index=True,
                         width="stretch")

    with tabs[1]:
        edges = gov("lineage")
        nodes = sorted(set(edges["from_node"]) | set(edges["to_node"])) if not edges.empty else []
        default = nodes.index("gold.customer_360") if "gold.customer_360" in nodes else 0
        focus = st.selectbox("Trace lineage for", ["(entire estate)"] + nodes, index=default + 1 if nodes else 0)
        sub = lin.subgraph(edges, None if focus == "(entire estate)" else focus)
        if not sub.empty:
            st.graphviz_chart(lin.to_dot(sub, focus), width="stretch")
            st.dataframe(sub[["from_node", "to_node", "transformation", "run_id"]], hide_index=True, width="stretch")

    with tabs[2]:
        st.markdown("**Roles**")
        st.dataframe(pd.DataFrame([{"role": k, **{x: (", ".join(v) if isinstance(v, list) else v) for x, v in r.items()}}
                                   for k, r in policy.roles.items()]), hide_index=True, width="stretch")
        st.markdown("**Access matrix** (✅ readable · 🔒 readable with masking · ❌ denied)")
        data_tables = cat[cat["layer"].isin(["silver", "gold"]) & ~cat["table_name"].str.startswith("quarantine__")]
        levels = list(CLASS_COLORS)
        matrix = {}
        for rname in policy.roles:
            col = []
            for t in data_tables.itertuples():
                if not policy.can_access(rname, t.layer, t.domain):
                    col.append("❌")
                elif levels.index(t.classification) > policy.clearance(rname):
                    col.append("🔒")
                else:
                    col.append("✅")
            matrix[rname] = col
        st.dataframe(pd.DataFrame(matrix, index=data_tables["fq_name"]), width="stretch")

        st.markdown(f"**Governed query sandbox** - runs as role **{role}**")
        sql = st.text_area("SQL (reference tables as layer.table)",
                           "SELECT customer_id, first_name, last_name, email_address, segment, lifetime_value\n"
                           "FROM gold.customer_360 ORDER BY lifetime_value DESC LIMIT 10", height=110)
        if st.button("Run governed query"):
            try:
                df, info = policy.governed_query(wh, role, sql, cat, cols)
                st.caption(f"Tables: {', '.join(info['tables'])} · masked: {', '.join(info['masked_columns']) or 'none'}")
                st.dataframe(df, hide_index=True, width="stretch")
            except AccessDenied as e:
                st.error(f"🚫 Access denied: {e}")
            except Exception as e:
                st.error(f"SQL error: {e}")

        st.markdown("**Native BigQuery enforcement** - the same policies as DCL / DDL / policy tags")
        script = bigquery_governance_script(suite.cfg, cat, cols)
        st.download_button("⬇ Download BigQuery governance script", script, "edms_bigquery_governance.sql")
        with st.expander("Preview script"):
            st.code(script[:12000], language="sql")

    with tabs[3]:
        sens = cols[cols["classification"].isin(["CONFIDENTIAL", "RESTRICTED"]) & ~cols["table_name"].str.startswith("quarantine__")]
        c = st.columns(3)
        c[0].metric("Sensitive columns", len(sens))
        c[1].metric("RESTRICTED", int((sens["classification"] == "RESTRICTED").sum()))
        c[2].metric("Tables with personal data", sens[["layer", "table_name"]].drop_duplicates().shape[0])
        if not sens.empty:
            agg = sens.groupby(["pii_type", "classification"], dropna=False).size().reset_index(name="columns")
            agg["pii_type"] = agg["pii_type"].fillna("propagated")
            st.plotly_chart(px.bar(agg, x="pii_type", y="columns", color="classification", height=320,
                                   color_discrete_map=CLASS_COLORS), width="stretch")
            st.dataframe(sens[["layer", "table_name", "column_name", "pii_type", "classification", "description"]],
                         hide_index=True, width="stretch")

    with tabs[4]:
        dq = gov("dq_results")
        if dq.empty:
            st.info("No DQ results yet.")
        else:
            last = dq[dq["run_id"] == dq["run_id"].max()]
            score = (last.groupby(["layer", "table_name"])["passed"].mean() * 100).round(1).reset_index(name="score")
            c = st.columns(3)
            c[0].metric("Overall DQ score", f"{round(100 * last['passed'].mean(), 1)}%")
            c[1].metric("Checks", len(last))
            c[2].metric("Failed checks", int((~last["passed"]).sum()))
            st.plotly_chart(px.bar(score, x="table_name", y="score", color="layer", height=320, range_y=[0, 100]), width="stretch")
            dims = (last.groupby("dimension")["passed"].mean() * 100).round(1).reset_index(name="score")
            st.dataframe(dims, hide_index=True)
            st.markdown("**Failed checks**")
            st.dataframe(last[~last["passed"]].drop(columns=["run_id"]), hide_index=True, width="stretch")
            trend = dq.groupby("run_id")["passed"].mean().mul(100).round(1).reset_index(name="score")
            if len(trend) > 1:
                st.plotly_chart(px.line(trend, x="run_id", y="score", markers=True, height=260), width="stretch")

    with tabs[5]:
        audit = gov("audit_log")
        if not audit.empty:
            a = st.columns(3)
            act = a[0].multiselect("Action", sorted(audit["action"].unique()))
            sts = a[1].multiselect("Status", sorted(audit["status"].unique()))
            rl = a[2].multiselect("Role", sorted(audit["role"].unique()))
            v = audit
            if act: v = v[v["action"].isin(act)]
            if sts: v = v[v["status"].isin(sts)]
            if rl: v = v[v["role"].isin(rl)]
            st.dataframe(v.sort_values("event_time", ascending=False), hide_index=True, width="stretch")


{"🏠 Overview": page_overview, "⚙️ Intelligent ETL": page_etl, "💬 Meta RAG": page_rag,
 "🛡️ Data Governance": page_governance}[page]()
