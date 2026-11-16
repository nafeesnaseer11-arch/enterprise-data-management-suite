"""Meta RAG chatbot: retrieval over metadata + data, governed SQL tool, Claude as the LLM.

Flow per question:
  1. embed the question and retrieve the top-k documents the user's role may see
  2. Claude answers from that context and may call tools:
       - search_knowledge: retrieve more metadata/rows
       - run_sql: execute a read-only query through the policy engine (access check,
         PII masking, audit log)
  3. the UI shows the answer, the cited sources and every SQL query with its results

Without an API key the bot runs in offline mode: retrieval + a heuristic SQL preview.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd

from edms.governance import store
from edms.governance.policies import AccessDenied

SYSTEM_PROMPT = """You are the Enterprise Data Management Suite assistant. You answer questions about the \
organisation's data, which was migrated from AWS into a BigQuery medallion architecture \
(bronze = raw, silver = cleaned, gold = business aggregates).

How to work:
- The user message contains <context> documents retrieved from the data catalog, business glossary and data rows. \
Use them to find the right tables and columns. Cite documents you rely on as [n].
- For any number, ranking, total or list, call run_sql rather than estimating from the context. Prefer gold tables, \
then silver. Never query bronze unless asked about raw data.
- Write SQL in {dialect} dialect and refer to tables ONLY as layer.table (e.g. gold.customer_360) - never add \
project or dataset prefixes. Only single SELECT statements are allowed. Aggregate in SQL; don't pull raw rows to count them.
- Results are governed for the user's role. Values like tok_xxxx or ***REDACTED*** are masked by policy: say the data is \
masked for their role; do not try to work around it. If a query is denied, explain the access rule.
- Use search_knowledge when the context does not contain the table or term you need.
- Answer concisely with the key numbers first. Use a small markdown table when listing several rows. \
If the data cannot answer the question, say so."""

TOOLS = [
    {
        "name": "run_sql",
        "description": "Run one read-only SELECT query against the governed warehouse and get up to 50 rows back as JSON. "
                       "Reference tables as layer.table, e.g. gold.revenue_by_month.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A single SELECT statement"},
                "purpose": {"type": "string", "description": "One sentence on what the query answers"},
            },
            "required": ["sql", "purpose"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_knowledge",
        "description": "Semantic search over the data catalog, business glossary and indexed data rows.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]


def llm_credentials_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
                or (Path.home() / ".config" / "anthropic").exists())


class MetaRAGChatbot:
    def __init__(self, cfg: dict, wh, policy, embedder, vstore):
        self.cfg, self.wh, self.policy, self.embedder, self.vstore = cfg, wh, policy, embedder, vstore
        self.llm_cfg = cfg["rag"]["llm"]
        self._client = None

    # ---------------------------------------------------------------- status
    @property
    def llm_enabled(self) -> bool:
        return self.llm_cfg["provider"] == "anthropic" and llm_credentials_available()

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    # ---------------------------------------------------------------- retrieval
    def _catalog(self):
        return store.read(self.wh, "catalog_tables"), store.read(self.wh, "catalog_columns")

    def retrieve(self, query: str, role: str, k: int | None = None) -> list[dict]:
        tables, _ = self._catalog()
        allowed = set(self.policy.accessible(role, tables)["fq_name"]) if not tables.empty else set()
        try:
            qv = self.embedder.embed([query], task="RETRIEVAL_QUERY")[0]
        except TypeError:
            qv = self.embedder.embed([query])[0]
        return self.vstore.search(qv, k or self.cfg["rag"]["top_k"], allowed)

    # ---------------------------------------------------------------- tools
    def _run_sql(self, role: str, sql: str, trace: list) -> tuple[str, bool]:
        tables, columns = self._catalog()
        try:
            df, info = self.policy.governed_query(self.wh, role, sql, tables, columns, limit=200)
            trace.append({"sql": sql, "result": df, "info": info})
            payload = {"rows": json.loads(df.head(50).to_json(orient="records", date_format="iso")),
                       "row_count": len(df), "masked_columns": info["masked_columns"]}
            return json.dumps(payload, default=str), False
        except AccessDenied as e:
            trace.append({"sql": sql, "error": f"ACCESS DENIED: {e}"})
            return f"ACCESS DENIED: {e}", True
        except Exception as e:  # SQL error -> let the model fix the query
            trace.append({"sql": sql, "error": str(e)})
            return f"SQL error: {e}", True

    # ---------------------------------------------------------------- answer
    def ask(self, question: str, role: str, history: list[dict] | None = None) -> dict:
        docs = self.retrieve(question, role)
        result = {"question": question, "role": role, "sources": docs, "queries": []}
        if self.llm_enabled:
            try:
                result.update(self._ask_claude(question, role, docs, history or [], result["queries"]))
            except Exception as e:  # network / auth / rate-limit -> degrade gracefully
                result.update(self._ask_offline(question, role, docs, result["queries"]))
                result["answer"] = f"_LLM unavailable ({type(e).__name__}: {e}); offline answer:_\n\n" + result["answer"]
        else:
            result.update(self._ask_offline(question, role, docs, result["queries"]))
        store.audit(self.wh, role, "CHAT", question[:200], "OK",
                    {"mode": result["mode"], "sources": [d["id"] for d in docs], "queries": len(result["queries"])})
        return result

    def _ask_claude(self, question, role, docs, history, trace) -> dict:
        context = "\n\n".join(f"[{i + 1}] {d['text']}" for i, d in enumerate(docs))
        r = self.policy.role(role)
        msgs = [{"role": m["role"], "content": m["content"]} for m in history[-8:]]
        msgs.append({"role": "user", "content":
                     f"<context>\n{context}\n</context>\n\n<user_role name=\"{role}\" layers=\"{r['layers']}\" "
                     f"domains=\"{r['domains']}\" clearance=\"{r['max_classification']}\"/>\n\nQuestion: {question}"})
        system = SYSTEM_PROMPT.format(dialect="BigQuery Standard SQL" if self.wh.dialect == "bigquery" else "DuckDB")
        extra_docs = []
        resp = None
        for _ in range(self.llm_cfg.get("max_tool_iterations", 6)):
            resp = self.client.beta.messages.create(
                model=self.llm_cfg["model"],
                max_tokens=16000,
                system=system,
                tools=TOOLS,
                messages=msgs,
                output_config={"effort": self.llm_cfg.get("effort", "medium")},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # on a policy decline, re-run on a fallback model automatically
            )
            if resp.stop_reason == "refusal":
                return {"answer": "The model declined to answer this request.", "mode": f"llm:{resp.model}"}
            msgs.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                break
            results = []
            for b in resp.content:
                if b.type != "tool_use":
                    continue
                if b.name == "run_sql":
                    out, err = self._run_sql(role, b.input.get("sql", ""), trace)
                elif b.name == "search_knowledge":
                    more = self.retrieve(b.input.get("query", ""), role, k=5)
                    extra_docs += more
                    out, err = "\n\n".join(f"[{d['id']}] {d['text']}" for d in more) or "No results", False
                else:
                    out, err = f"Unknown tool {b.name}", True
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": err})
            msgs.append({"role": "user", "content": results})
        answer = "".join(getattr(b, "text", "") for b in resp.content if b.type == "text").strip() if resp else ""
        if resp is not None and resp.stop_reason == "tool_use":
            answer += "\n\n_(Stopped after the maximum number of tool calls.)_"
        return {"answer": answer or "(no answer)", "mode": f"llm:{resp.model if resp else self.llm_cfg['model']}",
                "extra_sources": extra_docs}

    # ---------------------------------------------------------------- offline
    def _ask_offline(self, question, role, docs, trace) -> dict:
        lines = ["**Offline mode** (set `ANTHROPIC_API_KEY` to enable Claude answers). "
                 "Most relevant knowledge for your question:"]
        for g in [d for d in docs if d["meta"]["kind"] == "glossary"][:2]:
            lines.append(f"- 📘 {g['text']}")
        table_docs = [d for d in docs if d["meta"].get("fq")]
        seen, fqs = set(), []
        for d in table_docs:
            if d["meta"]["fq"] not in seen:
                seen.add(d["meta"]["fq"])
                fqs.append(d["meta"]["fq"])
        tables, columns = self._catalog()
        for fq in fqs[:3]:
            row = tables[tables["fq_name"] == fq]
            if not row.empty:
                lines.append(f"- 🗂️ `{fq}` - {row.iloc[0]['description']} ({int(row.iloc[0]['row_count']):,} rows)")
        if fqs:
            fq = next((f for f in fqs if f.startswith("gold.")), fqs[0])
            sql = self._heuristic_sql(question, fq, columns)
            out, err = self._run_sql(role, sql, trace)
            lines.append(f"\nI ran a query on `{fq}`" + (" but it was blocked or failed." if err else " - see the result below."))
        else:
            lines.append("\nNo accessible tables matched your question for this role.")
        return {"answer": "\n".join(lines), "mode": "offline"}

    @staticmethod
    def _heuristic_sql(question: str, fq: str, columns: pd.DataFrame) -> str:
        layer, table = fq.split(".", 1)
        cols = columns[(columns["layer"] == layer) & (columns["table_name"] == table)]
        q = set(re.findall(r"[a-z]+", question.lower()))
        if {"how", "many"} <= q or "count" in q:
            return f"SELECT COUNT(*) AS row_count FROM {fq}"
        numeric = cols[cols["data_type"].isin(["INT64", "FLOAT64"]) & ~cols["column_name"].str.endswith("_id")]
        best, best_score = None, 0
        for c in numeric["column_name"]:
            s = len(set(c.split("_")) & q)
            if s > best_score:
                best, best_score = c, s
        if best is None and not numeric.empty and q & {"top", "highest", "most", "best", "largest", "biggest"}:
            best = numeric["column_name"].iloc[-1]
        order = f" ORDER BY {best} {'ASC' if q & {'lowest', 'least', 'bottom', 'smallest'} else 'DESC'}" if best else ""
        return f"SELECT * FROM {fq}{order} LIMIT 10"
