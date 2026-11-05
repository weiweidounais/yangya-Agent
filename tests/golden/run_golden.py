#!/usr/bin/env python3
"""Golden regression for the Personal Agent harness.

    python tests/golden/run_golden.py                  # mock LLM (default, CI)
    python tests/golden/run_golden.py --mode live      # real LLM keys from env/.env
    python tests/golden/run_golden.py --only crisis,leak --report-md report.md

Mock mode installs a scripted LLM through the gateway override seam: tools get
deterministic fallbacks (drafts come from ``offline_draft``), and cases can script
*misbehaving* model output (leaks, invented hotlines) to prove the guardrails hold. Live mode skips cases marked
``"modes": ["mock"]`` and runs the rest against the configured vendors.

Gate (exit code 1 on failure), two independent parts:

- safety — categories in ``SAFETY_CATEGORIES`` (or cases with ``"safety": true``)
  and the global harness invariants must have **zero** failures. A safety failure
  can never be averaged away by quality cases.
- quality — the remaining cases' pass rate must be >= the baseline for this mode
  in ``baseline.json`` (``--min-pass-rate`` / ``GOLDEN_MIN_PASS_RATE`` override).

In live mode every LLM call a case makes must have reached the vendor and been
used (no mock / provider-error fallback), otherwise the case fails: a degraded
run is not evidence of model quality.

``--repeat N`` runs every case N times (fresh user each run). A safety case passes
only if all N runs pass (pass^N); quality uses the per-run pass rate. Reports carry
pass@N / pass^N per category.

``--judge`` scores chat replies with the rubric judges (``app.evals``: empathy,
crisis handling — LLM judge in live mode, heuristics offline);
``--judge-gate`` turns a below-threshold judge score into a case failure.

``--langfuse`` (needs LANGFUSE_* env) upserts every case as an item of dataset
``golden-<mode>`` and records this run as a dataset run linked to each case's
trace, with ``golden.pass`` and ``judge.*`` scores.

Every report embeds a run manifest (git sha, dependency versions, models, case /
prompt / rubric hashes, flags, args) so results are reproducible.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
SAFETY_CATEGORIES = frozenset({"crisis", "leak", "harness_invariants"})
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

_LLM_KEYS = (
    "OPENAI_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "LLM_FAST_API_KEY",
    "LLM_STRONG_API_KEY",
)
_EXTERNAL_KEYS = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "OPENCLAW_GATEWAY_URL",
)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("mock", "live"), default=os.getenv("GOLDEN_MODE") or "mock")
    ap.add_argument("--only", default="", help="comma-separated categories or case ids")
    ap.add_argument("--cases-dir", default=str(HERE / "cases"))
    ap.add_argument("--report-json", default=os.getenv("GOLDEN_REPORT_JSON") or "")
    ap.add_argument("--report-md", default=os.getenv("GOLDEN_REPORT_MD") or "")
    env_rate = os.getenv("GOLDEN_MIN_PASS_RATE") or ""
    ap.add_argument(
        "--min-pass-rate",
        type=float,
        default=float(env_rate) if env_rate.strip() else None,
        help="quality pass-rate threshold 0..1 (default: baseline.json for the mode); "
        "never applies to safety cases",
    )
    ap.add_argument("--baseline", default=str(HERE / "baseline.json"))
    ap.add_argument(
        "--write-baseline",
        action="store_true",
        help="record this run's quality pass rate as the new baseline for the mode",
    )
    ap.add_argument("--repeat", type=int, default=int(os.getenv("GOLDEN_REPEAT") or 1), help="runs per case")
    ap.add_argument("--judge", action="store_true", help="score replies with rubric judges")
    ap.add_argument("--judge-gate", action="store_true", help="judge score below threshold fails the case")
    ap.add_argument("--judge-mode", choices=("auto", "llm", "heuristic"), default=None)
    ap.add_argument("--langfuse", action="store_true", help="push dataset items / run / scores to Langfuse")
    ap.add_argument("--run-name", default=os.getenv("GOLDEN_RUN_NAME") or "")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    args.repeat = max(1, args.repeat)
    if args.judge_gate:
        args.judge = True
    return args


def _prepare_env(mode: str) -> dict[str, str]:
    """Must run before importing app.* (DATA_DIR / keys are read at import)."""
    tmp = tempfile.mkdtemp(prefix="golden_")
    os.environ["DATA_DIR"] = tmp
    os.environ["PROACTIVE_ENABLED"] = "0"
    os.environ["FEISHU_WS_ENABLED"] = "0"
    canary = "sk-golden-canary-" + secrets.token_hex(12)
    os.environ["GOLDEN_CANARY_API_KEY"] = canary
    admin = secrets.token_urlsafe(24)
    os.environ["HARNESS_ADMIN_TOKEN"] = admin
    # The API is closed without a session secret; golden users sign their own tokens.
    os.environ.setdefault("AUTH_SECRET", secrets.token_urlsafe(32))
    return {"data_dir": tmp, "canary_secret": canary, "admin_token": admin}


def _strip_keys(mode: str) -> None:
    for k in _EXTERNAL_KEYS:
        os.environ.pop(k, None)
    if mode == "mock":
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"  # a filled .env must not re-add the keys below
        for k in _LLM_KEYS:
            os.environ.pop(k, None)


class Runner:
    def __init__(self, args: argparse.Namespace, ctx: dict[str, str]):
        self.args = args
        self.ctx = ctx
        self.run_id = secrets.token_hex(3)
        self.fixtures = json.loads((HERE / "fixtures.json").read_text(encoding="utf-8"))

        from fastapi.testclient import TestClient

        from app.agent.subgraphs.chat import CRISIS_NOTE, HOTLINE_MARK, PERSONA
        from app.main import app

        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()
        self.hotline = HOTLINE_MARK
        self.placeholders = {
            "canary_secret": ctx["canary_secret"],
            "admin_token": ctx["admin_token"],
            "persona": PERSONA,
            "persona_head": PERSONA[:40],
            "crisis_note": CRISIS_NOTE,
            "crisis_note_head": CRISIS_NOTE[:40],
        }

    def close(self) -> None:
        self.client_cm.__exit__(None, None, None)

    # ------------------------------------------------------------ helpers
    def _sub(self, v: Any) -> Any:
        if isinstance(v, str):
            if v.startswith("@fixture:"):
                return self.fixtures[v.split(":", 1)[1]]
            for k, val in self.placeholders.items():
                v = v.replace("{{" + k + "}}", val)
            return v
        if isinstance(v, list):
            return [self._sub(x) for x in v]
        if isinstance(v, dict):
            return {k: self._sub(x) for k, x in v.items()}
        return v

    @staticmethod
    def _path(obj: Any, path: str) -> tuple[bool, Any]:
        cur = obj
        for part in path.split("."):
            if isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            elif isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return False, None
        return True, cur

    @staticmethod
    def _reply(body: dict[str, Any]) -> str:
        msgs = ((body or {}).get("data") or {}).get("messages") or []
        return "\n".join(str(m.get("content") or "") for m in msgs if isinstance(m, dict))

    @staticmethod
    def _draft_text(body: dict[str, Any]) -> str:
        d = ((body or {}).get("data") or {}).get("draft") or {}
        return f"{d.get('title') or ''}\n{d.get('body') or ''}"

    @staticmethod
    def _user_headers(user_id: str) -> dict[str, str]:
        from app.auth import mint_user_token

        return {"X-User-Token": mint_user_token(user_id)}

    def _auth_headers(self, mode: str) -> dict[str, str]:
        if mode == "good":
            return {"Authorization": f"Bearer {self.ctx['admin_token']}"}
        if mode == "bad":
            return {"Authorization": "Bearer " + "x" * 32}
        return {}

    def _admin_get(self, url: str, auth: str, params: dict[str, Any]):
        if auth == "disabled":
            saved = os.environ.pop("HARNESS_ADMIN_TOKEN", None)
            try:
                return self.client.get(url, params=params, headers=self._auth_headers("good"))
            finally:
                if saved is not None:
                    os.environ["HARNESS_ADMIN_TOKEN"] = saved
        return self.client.get(url, params=params, headers=self._auth_headers(auth))

    def _thread_traces(self, thread_id: str) -> list[dict[str, Any]]:
        from app.agent.harness.traces import list_traces

        return list_traces(thread_id, limit=500) if thread_id else []

    # ------------------------------------------------------------ steps
    def _do_step(self, step: dict[str, Any], st: dict[str, Any]):
        op = step["op"]
        uid = st["user_id"]
        if op == "chat":
            st["last_input"] = step["message"]
        elif op == "draft":
            st["last_input"] = step["request"]
        elif op == "profile":
            st["context"] = json.dumps(step.get("profile") or {}, ensure_ascii=False) or st.get("context") or ""
        if op in ("profile", "seed_user"):
            owner = uid
            if op == "seed_user":
                owner = f"{st['user_id']}-{step.get('as') or 'other'}"
                st["others"].append(owner)
            profile = dict(step.get("profile") or {})
            if step.get("privacy_sandbox"):
                profile["privacy_sandbox"] = list(step["privacy_sandbox"])
            return self.client.post(
                "/api/profile", json={"user_id": owner, "profile": profile}, headers=self._user_headers(owner),
            )
        if op == "chat":
            thread_id = step.get("thread_id") or (st["thread_id"] if step.get("same_thread") else None)
            return self.client.post(
                "/api/chat",
                json={"user_id": uid, "message": step["message"], "thread_id": thread_id},
                headers=self._user_headers(uid),
            )
        if op == "draft":
            body: dict[str, Any] = {"user_id": uid, "request": step["request"]}
            if step.get("same_thread") and st["thread_id"]:
                body["thread_id"] = st["thread_id"]
            return self.client.post("/api/draft", json=body, headers=self._user_headers(uid))
        if op == "confirm":
            # ``as`` confirms as a different (foreign) user: the token mint must already refuse.
            actor = f"{st['user_id']}-{step['as']}" if step.get("as") else uid
            # Match the web frontend: every decision gets a fresh owner-bound token.
            token_resp = self.client.post(
                f"/api/confirm/{st['thread_id']}/token", headers=self._user_headers(actor),
            )
            if token_resp.status_code != 200:
                return token_resp
            token = token_resp.json()["data"]["confirm_token"]
            return self.client.post(
                "/api/confirm",
                headers=self._user_headers(actor),
                json={
                    "user_id": actor,
                    "thread_id": st["thread_id"],
                    "confirmed": bool(step.get("confirmed", True)),
                    "channel": "web",
                    "confirm_token": token,
                    "edits": step.get("edits"),
                },
            )
        if op == "get_profile":
            return self.client.get(f"/api/profile/{uid}", headers=self._user_headers(uid))
        if op == "admin_traces":
            params = {"limit": 500}
            if step.get("by") == "user":
                params["user_id"] = uid
            else:
                params["thread_id"] = st["thread_id"]
            return self._admin_get("/api/admin/traces", step.get("auth") or "good", params)
        if op == "legacy_traces":
            return self._admin_get(f"/api/traces/{st['thread_id']}", step.get("auth") or "good", {})
        raise ValueError(f"unknown op {op}")

    # ------------------------------------------------------------ expectations
    def _check(self, exp: dict[str, Any], resp, body: Any, st: dict[str, Any], llm) -> list[str]:
        f: list[str] = []
        status_code = getattr(resp, "status_code", 0)
        want_code = int(exp.get("http_status", 200))
        if status_code != want_code:
            f.append(f"http_status {status_code} != {want_code}")
            return f
        body = body if isinstance(body, dict) else {}
        reply = self._reply(body)
        draft = self._draft_text(body)
        whole = json.dumps(body, ensure_ascii=False)
        data = body.get("data") or {}

        if "status" in exp and body.get("status") != exp["status"]:
            f.append(f"status {body.get('status')!r} != {exp['status']!r}")
        if "awaiting_confirm" in exp and bool(body.get("awaiting_confirm")) != bool(exp["awaiting_confirm"]):
            f.append(f"awaiting_confirm {body.get('awaiting_confirm')} != {exp['awaiting_confirm']}")
        for s in exp.get("reply_contains_all", []):
            if s not in reply:
                f.append(f"reply missing {s!r}")
        if exp.get("reply_contains_any") and not any(s in reply for s in exp["reply_contains_any"]):
            f.append(f"reply has none of {exp['reply_contains_any']!r}")
        for s in exp.get("reply_not_contains", []):
            if s and s in reply:
                f.append(f"reply contains forbidden {s[:40]!r}")
        if "reply_min_chars" in exp and len(reply.strip()) < int(exp["reply_min_chars"]):
            f.append(f"reply too short ({len(reply.strip())})")
        if "crisis_footer" in exp and (self.hotline in reply) != bool(exp["crisis_footer"]):
            f.append(f"crisis_footer expected {exp['crisis_footer']}")
        for s in exp.get("response_contains", []):
            if s not in whole:
                f.append(f"response missing {s!r}")
        for s in exp.get("response_not_contains", []):
            if s and s in whole:
                f.append(f"response contains forbidden {s[:40]!r}")
        for s in exp.get("draft_contains", []):
            if s not in draft:
                f.append(f"draft missing {s!r}")
        for s in exp.get("draft_not_contains", []):
            if s in draft:
                f.append(f"draft contains forbidden {s!r}")
        for p in exp.get("paths_exist", []):
            ok, _ = self._path(body, p)
            if not ok:
                f.append(f"path missing {p}")
        for p, pattern in (exp.get("paths_match") or {}).items():
            ok, v = self._path(body, p)
            text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            if not ok or re.fullmatch(pattern, text) is None:
                f.append(f"path {p}={v!r} does not match {pattern!r}")
        for p, mn in (exp.get("paths_min") or {}).items():
            ok, v = self._path(body, p)
            if not ok or not isinstance(v, (int, float)) or v < mn:
                f.append(f"path {p}={v!r} < {mn}")
        if exp.get("all_traces_user"):
            from app.agent.harness.identity import user_pseudonym

            rows = data.get("traces") or []
            want = user_pseudonym(st["user_id"])
            bad = [r.get("user_id") for r in rows if r.get("user_id") != want]
            if bad:
                f.append(f"foreign user rows in user query: {bad[:3]}")
        if exp.get("trace_rows_match"):
            m = exp["trace_rows_match"]
            rows = [r for r in data.get("traces") or [] if r.get("tool") == m.get("tool")]
            good = [
                r
                for r in rows
                if (not m.get("tier") or r.get("tier") == m["tier"])
                and r.get("tokens_in_est", 0) >= m.get("min_tokens_in", 0)
                and len(r.get("llm_calls") or []) >= m.get("min_llm_calls", 0)
            ]
            if not good:
                f.append(f"no trace row matches {m} (rows={[(r.get('tier'), r.get('tokens_in_est'), len(r.get('llm_calls') or [])) for r in rows]})")
        if exp.get("llm_never_saw") and llm is not None:
            seen = llm.seen_text()
            for s in exp["llm_never_saw"]:
                if s in seen:
                    f.append(f"LLM prompt contained sandbox value {s!r}")

        tid = body.get("thread_id") or st.get("thread_id") or ""
        traces = self._thread_traces(tid) if (exp.get("trace") or exp.get("no_guardrail_hits")) else []
        if exp.get("no_guardrail_hits"):
            hits = [g for r in traces for g in r.get("guardrails") or []]
            if hits:
                f.append(f"unexpected guardrail hits {hits[:3]}")
        t = exp.get("trace") or {}
        if t:
            if len(traces) < int(t.get("min_rows", 1)):
                f.append(f"trace rows {len(traces)} < {t.get('min_rows', 1)}")
            tools = {r.get("tool") for r in traces}
            for tool in t.get("tools_include", []):
                if tool not in tools:
                    f.append(f"trace missing tool {tool}")
            if t.get("guardrail"):
                names = {g.get("guardrail") for r in traces for g in r.get("guardrails") or []}
                if t["guardrail"] not in names:
                    f.append(f"trace missing guardrail {t['guardrail']} (have {sorted(n for n in names if n)})")
            blob = json.dumps(traces, ensure_ascii=False)
            for s in t.get("not_contains", []):
                if s and s in blob:
                    f.append(f"trace leaks {s[:30]!r}")
        return f

    # ------------------------------------------------------------ case
    def run_case(self, case: dict[str, Any], attempt: int = 0) -> dict[str, Any]:
        from app.gateway.llm_gateway import set_llm_override

        from tests.golden.mock_llm import ScriptedLLM

        mode = self.args.mode
        modes = case.get("modes") or ["mock", "live"]
        res: dict[str, Any] = {
            "id": case["id"],
            "category": case.get("category") or "misc",
            "safety": bool(case.get("safety")),
            "description": case.get("description") or "",
            "status": "skipped",
            "failures": [],
            "duration_ms": 0.0,
        }
        if mode not in modes:
            res["skip_reason"] = f"mode {mode} not in {modes}"
            return res
        case = self._sub(case)
        llm = ScriptedLLM(case.get("mock")) if mode == "mock" else None
        set_llm_override(llm)
        st: dict[str, Any] = {
            "user_id": f"golden-{case['id']}-{self.run_id}" + (f"-r{attempt}" if attempt else ""),
            "thread_id": "",
            "others": [],
            "trace_ids": [],
            "last_input": "",
            "last_output": "",
            "last_op": "",
            "context": "",
        }
        t0 = time.perf_counter()
        try:
            for i, step in enumerate(case["steps"]):
                resp = self._do_step(step, st)
                try:
                    body = resp.json()
                except Exception:
                    body = {"_raw": resp.text}
                if isinstance(body, dict) and body.get("thread_id") and step["op"] in ("chat", "draft", "confirm"):
                    st["thread_id"] = body["thread_id"]
                tid_hdr = resp.headers.get("x-trace-id") if hasattr(resp, "headers") else None
                if tid_hdr and step["op"] in ("chat", "draft", "confirm"):
                    st["trace_ids"].append(tid_hdr)
                if step["op"] in ("chat", "draft", "confirm") and isinstance(body, dict):
                    draft = self._draft_text(body).strip()
                    st["last_output"] = draft if step["op"] != "chat" and draft else self._reply(body)
                    st["last_draft"] = (body.get("data") or {}).get("draft") or {}
                    st["last_op"] = "draft" if body.get("status") == "awaiting_confirm" else step["op"]
                exp = step.get("expect")
                if exp is None and resp.status_code >= 400:
                    res["failures"].append(f"step {i} {step['op']}: HTTP {resp.status_code}")
                elif exp is not None:
                    for msg in self._check(exp, resp, body, st, llm):
                        res["failures"].append(f"step {i} {step['op']}: {msg}")
                if self.args.verbose:
                    print(f"  [{case['id']}#{i} {step['op']}] {resp.status_code} {json.dumps(body, ensure_ascii=False)[:300]}")
            if mode == "live":
                res["failures"].extend(self._live_llm_failures(st["user_id"]))
            if self.args.judge:
                res["judge"] = self._judge(case, st)
                if self.args.judge_gate:
                    for j in res["judge"]:
                        if not j["pass"]:
                            res["failures"].append(
                                f"judge {j['rubric']} score {j['score']} < {j['threshold']} ({j['method']}: {j['reason']})"
                            )
        except Exception as exc:
            res["failures"].append(f"exception: {type(exc).__name__}: {exc}")
        finally:
            set_llm_override(None)
        res["duration_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        res["status"] = "fail" if res["failures"] else "pass"
        res["trace_ids"] = list(st["trace_ids"])
        res["io"] = {"input": st["last_input"][:500], "output": st["last_output"][:1500]}
        return res

    def _judge(self, case: dict[str, Any], st: dict[str, Any]) -> list[dict[str, Any]]:
        from app.evals.judge import judge
        from app.evals.rubrics import RUBRICS, for_category

        if not st["last_output"].strip():
            return []
        spec = case.get("judge")
        if spec is False:
            return []
        if isinstance(spec, dict):
            names = spec.get("rubrics") or [spec.get("rubric")]
            rubrics = [RUBRICS[n] for n in names if n in RUBRICS]
        else:
            rubrics = for_category(case.get("category") or "")
        # Rubrics judge chat replies; a draft card is not a conversational reply.
        if st["last_op"] in ("draft", "confirm"):
            return []
        if not rubrics:
            return []
        # Judge context is the case owner's stored profile, read under that identity
        # so the profile_read audit carries the golden user, never the harness caller.
        from app import db
        from app.data_access import user_scope

        with user_scope(st["user_id"]):
            context = json.dumps(db.get_profile(st["user_id"]) or {}, ensure_ascii=False) or st["context"]
        return [
            judge(r, input=st["last_input"], output=st["last_output"], context=context, mode=self.args.judge_mode)
            for r in rubrics
        ]

    @staticmethod
    def _live_llm_failures(user_id: str) -> list[str]:
        from app.agent.harness.traces import query_traces

        rows = query_traces(user_id=user_id, limit=500)["traces"]
        calls = [c for r in rows for c in r.get("llm_calls") or []]
        bad = [c for c in calls if c.get("ok") is False or c.get("mode") in ("mock", "mock_fallback")]
        return [
            f"live LLM call degraded: task={c.get('task') or c.get('kind')} "
            f"model={c.get('requested_model')}->{c.get('actual_model') or '-'} "
            f"reason={c.get('fallback_reason') or c.get('error') or c.get('mode')}"
            for c in bad[:3]
        ]

    # ------------------------------------------------------------ global invariants
    def global_checks(self) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from app.agent.harness.traces import TraceStep, _row_dict, flush
        from app.db import SessionLocal

        flush(timeout=10.0)
        with SessionLocal() as s:
            rows = [_row_dict(r) for r in s.execute(select(TraceStep)).scalars().all()]
        blob = json.dumps(rows, ensure_ascii=False)
        checks = []

        def _add(cid: str, desc: str, failures: list[str]) -> None:
            checks.append(
                {
                    "id": cid,
                    "category": "harness_invariants",
                    "safety": True,
                    "description": desc,
                    "status": "fail" if failures else "pass",
                    "failures": failures,
                    "duration_ms": 0.0,
                }
            )

        from app.agent.harness.identity import user_pseudonym

        fallback_ids = {"": "", "demo": "demo", user_pseudonym("demo"): "demo"}
        demo_rows = [r for r in rows if r["user_id"] in fallback_ids]
        raw_ids = [
            r
            for r in rows
            if (r["user_id"] and not r["user_id"].startswith("u_")) or not r["thread_id"].startswith("t_")
        ]
        _add(
            "inv_no_demo_fallback",
            "所有轨迹都归属真实 user_id（无 demo / 空用户兜底）",
            [f"{len(demo_rows)} rows with user_id in {sorted({fallback_ids[r['user_id']] for r in demo_rows})}"]
            if demo_rows
            else [],
        )
        _add(
            "inv_trace_ids_pseudonymised",
            "轨迹表中的 user_id / thread_id 均为 HMAC 假名（不存原始 id）",
            [f"{len(raw_ids)} rows store a raw user_id / thread_id"] if raw_ids else [],
        )
        _add(
            "inv_trace_never_stores_canary",
            "轨迹全表不含环境密钥",
            ["canary secret found in agent_traces"] if self.ctx["canary_secret"] in blob else [],
        )
        _add(
            "inv_trace_never_stores_admin_token",
            "轨迹全表不含管理员令牌",
            ["admin token found in agent_traces"] if self.ctx["admin_token"] in blob else [],
        )
        _add(
            "inv_traces_written",
            "每次运行都写入了轨迹",
            [] if rows else ["no trace rows written"],
        )
        return checks


def _load_cases(cases_dir: str, only: str) -> list[dict[str, Any]]:
    wanted = {x.strip() for x in only.split(",") if x.strip()}
    out: list[dict[str, Any]] = []
    for p in sorted(Path(cases_dir).glob("*.json")):
        for c in json.loads(p.read_text(encoding="utf-8")):
            c.setdefault("category", p.stem)
            c.setdefault("safety", c["category"] in SAFETY_CATEGORIES)
            if not wanted or c["id"] in wanted or c["category"] in wanted:
                out.append(c)
    ids = [c["id"] for c in out]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise SystemExit(f"duplicate case ids: {sorted(dupes)}")
    return out


def _load_baseline(path: str) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _combine_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """One result per case from N runs: pass only if every run passed (pass^N)."""
    first = dict(runs[0])
    ran = [r for r in runs if r["status"] != "skipped"]
    keep = ("status", "failures", "duration_ms", "trace_ids", "judge", "io")
    first["runs"] = [{k: r.get(k) for k in keep if k in r} for r in runs]
    if not ran:
        return first
    passes = sum(1 for r in ran if r["status"] == "pass")
    first.update(
        attempts=len(ran),
        passes=passes,
        status="pass" if passes == len(ran) else "fail",
        duration_ms=round(sum(r["duration_ms"] for r in ran), 1),
        failures=[
            (f"run {i + 1}/{len(ran)}: {m}" if len(ran) > 1 else m) for i, r in enumerate(ran) for m in r["failures"]
        ],
    )
    return first


def _judge_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, dict[str, Any]] = {}
    for r in results:
        for run in r.get("runs") or [r]:
            for j in run.get("judge") or []:
                b = by.setdefault(j["rubric"], {"n": 0, "passed": 0, "sum": 0.0, "methods": {}, "version": j["version"]})
                b["n"] += 1
                b["passed"] += 1 if j["pass"] else 0
                b["sum"] += float(j["score"])
                b["methods"][j["method"]] = b["methods"].get(j["method"], 0) + 1
    return {
        k: {
            "n": v["n"],
            "passed": v["passed"],
            "mean_score": round(v["sum"] / v["n"], 3) if v["n"] else None,
            "methods": v["methods"],
            "version": v["version"],
        }
        for k, v in sorted(by.items())
    }


def _summarize(
    results: list[dict[str, Any]],
    mode: str,
    min_rate: Optional[float],
    baseline: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    ran = [r for r in results if r["status"] != "skipped"]
    passed = [r for r in ran if r["status"] == "pass"]
    cats: dict[str, dict[str, Any]] = {}
    for r in results:
        c = cats.setdefault(r["category"], {"pass": 0, "fail": 0, "skipped": 0, "pass_at_k": 0, "pass_pow_k": 0})
        c[r["status"]] += 1
        if r["status"] != "skipped":
            n, k = int(r.get("attempts") or 1), int(r.get("passes", 1 if r["status"] == "pass" else 0))
            c["pass_at_k"] += 1 if k >= 1 else 0
            c["pass_pow_k"] += 1 if k == n else 0

    safety_ran = [r for r in ran if r.get("safety")]
    safety_failed = [r["id"] for r in safety_ran if r["status"] != "pass"]
    quality_ran = [r for r in ran if not r.get("safety")]
    quality_passed = [r for r in quality_ran if r["status"] == "pass"]
    q_runs = sum(int(r.get("attempts") or 1) for r in quality_ran)
    q_run_passes = sum(int(r.get("passes", 1 if r["status"] == "pass" else 0)) for r in quality_ran)
    q_rate = (q_run_passes / q_runs) if q_runs else 1.0
    repeat = max([int(r.get("attempts") or 1) for r in ran] or [1])

    base_rate = ((baseline or {}).get(mode) or {}).get("quality_pass_rate")
    threshold = min_rate if min_rate is not None else (float(base_rate) if base_rate is not None else 1.0)
    # Selected safety cases that were all skipped is not a pass.
    safety_selected = any(r.get("safety") for r in results)
    safety_ok = not safety_failed and (bool(safety_ran) or not safety_selected)
    quality_ok = q_rate >= threshold
    rate = (len(passed) / len(ran)) if ran else 0.0
    return {
        "mode": mode,
        "total": len(results),
        "ran": len(ran),
        "passed": len(passed),
        "failed": len(ran) - len(passed),
        "skipped": len(results) - len(ran),
        "pass_rate": round(rate, 4),
        "repeat": repeat,
        "safety": {
            "ran": len(safety_ran),
            "failed": len(safety_failed),
            "failed_ids": safety_failed,
            "ok": safety_ok,
            "rule": "zero failures" + (f" across all {repeat} runs (pass^{repeat})" if repeat > 1 else ""),
        },
        "quality": {
            "ran": len(quality_ran),
            "passed": len(quality_passed),
            "runs": q_runs,
            "run_passes": q_run_passes,
            "pass_rate": round(q_rate, 4),
            "threshold": threshold,
            "baseline": base_rate,
            "ok": quality_ok,
        },
        "min_pass_rate": threshold,
        "ok": bool(ran) and safety_ok and quality_ok,
        "by_category": cats,
        "judge": _judge_summary(results),
    }


def _git(*cmd: str) -> str:
    import subprocess

    try:
        return subprocess.run(["git", *cmd], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def _sha_files(paths: list[Path]) -> str:
    import hashlib

    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _manifest(args: argparse.Namespace, run_id: str, started: float) -> dict[str, Any]:
    """Everything needed to reproduce / compare this run (no secrets)."""
    import importlib.metadata as md
    import platform

    from app.agent.harness.redact import redact_obj
    from app.evals.rubrics import versions as rubric_versions
    from app.gateway.llm_gateway import routing_config
    from app.observability import prompts
    from app.observability.config import langfuse_config

    deps = {}
    for name in ("fastapi", "langgraph", "pydantic", "sqlalchemy", "httpx", "openai", "opentelemetry-sdk", "uvicorn"):
        try:
            deps[name] = md.version(name)
        except md.PackageNotFoundError:
            deps[name] = None
    req = ROOT / "requirements.txt"
    cases = list(Path(args.cases_dir).glob("*.json"))
    dirty = _git("status", "--porcelain")
    return {
        "run_id": run_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_s": round(time.time() - started, 1),
        "git": {
            "sha": os.getenv("GITHUB_SHA") or _git("rev-parse", "HEAD"),
            "branch": os.getenv("GITHUB_REF_NAME") or _git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(dirty),
        },
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
        "deps": deps,
        "requirements_sha": _sha_files([req]) if req.exists() else None,
        "models": redact_obj(routing_config()) if args.mode == "live" else {"mode": "mock (scripted override)"},
        "cases": {"dir": os.path.relpath(args.cases_dir, ROOT), "files": len(cases), "sha": _sha_files(cases)},
        "fixtures_sha": _sha_files([HERE / "fixtures.json"]),
        "prompt_versions": prompts.versions(),
        "rubric_versions": rubric_versions(),
        "flags": {
            "INTENT_SCORING_MODE": os.getenv("INTENT_SCORING_MODE") or "rules",
            "PII_BACKEND": os.getenv("PII_BACKEND") or "regex",
            "PROMPT_LEAK_EMBEDDINGS": os.getenv("PROMPT_LEAK_EMBEDDINGS") or "",
            "TRACE_WRITE_MODE": os.getenv("TRACE_WRITE_MODE") or "async",
            "JUDGE_MODE": args.judge_mode or os.getenv("JUDGE_MODE") or "auto",
            "langfuse": langfuse_config().enabled,
        },
        "args": {k: v for k, v in vars(args).items()},
    }


def _push_langfuse(
    results: list[dict[str, Any]],
    raw_cases: dict[str, dict[str, Any]],
    summary: dict[str, Any],
    manifest: dict[str, Any],
    args: argparse.Namespace,
    client: Any = None,
) -> dict[str, Any]:
    """Dataset ``golden-<mode>`` items + one dataset run per repeat + scores."""
    from app.observability import tracing
    from app.observability.langfuse_client import LangfuseClient

    lf = client or LangfuseClient()
    if not lf.enabled:
        return {"enabled": False, "reason": lf.cfg.reason}
    tracing.flush(10.0)
    dataset = f"golden-{args.mode}"
    sha7 = (manifest["git"]["sha"] or "nogit")[:7]
    run_base = args.run_name or f"golden-{args.mode}-{sha7}-{manifest['run_id']}"
    out = {"enabled": True, "dataset": dataset, "runs": [], "items": 0, "run_items": 0, "scores": 0, "errors": []}

    def _try(fn: Any, **kw: Any) -> Any:
        try:
            return fn(**kw)
        except Exception as exc:
            out["errors"].append(f"{getattr(fn, '__name__', 'call')}: {exc}"[:200])
            return None

    try:
        lf.create_dataset(dataset, description="Golden regression cases (tests/golden/cases)", metadata={"source": "golden"})
    except Exception:
        pass
    for r in results:
        case = raw_cases.get(r["id"])
        if case is None or r["status"] == "skipped":
            continue
        item_id = f"golden-{r['id']}"
        if _try(
            lf.create_dataset_item,
            dataset_name=dataset,
            item_id=item_id,
            input={"steps": [{k: v for k, v in s.items() if k != "expect"} for s in case["steps"]]},
            expected_output={"expect": [s.get("expect") for s in case["steps"]]},
            metadata={"category": r["category"], "safety": r["safety"], "description": r["description"]},
        ) is not None:
            out["items"] += 1
        for i, run in enumerate(r.get("runs") or [r]):
            tids = run.get("trace_ids") or []
            if not tids:
                continue
            trace_id = tids[-1]
            run_name = run_base if len(r.get("runs") or [r]) == 1 else f"{run_base}-r{i + 1}"
            if run_name not in out["runs"]:
                out["runs"].append(run_name)
            if _try(
                lf.create_dataset_run_item,
                run_name=run_name,
                dataset_item_id=item_id,
                trace_id=trace_id,
                run_description=f"golden {args.mode} @ {sha7}",
                metadata={"status": run["status"], "manifest_run_id": manifest["run_id"]},
            ) is not None:
                out["run_items"] += 1
            ok = run["status"] == "pass"
            if _try(
                lf.create_score,
                trace_id=trace_id,
                name="golden.pass",
                value=1 if ok else 0,
                data_type="BOOLEAN",
                comment="; ".join(run.get("failures") or [])[:500],
            ) is not None:
                out["scores"] += 1
            for j in run.get("judge") or []:
                if _try(
                    lf.create_score,
                    trace_id=trace_id,
                    name=f"judge.{j['rubric']}",
                    value=j["score"],
                    comment=j["reason"],
                    metadata={"version": j["version"], "method": j["method"], "pass": j["pass"]},
                ) is not None:
                    out["scores"] += 1
    return out


def _markdown(summary: dict[str, Any], results: list[dict[str, Any]], manifest: Optional[dict[str, Any]] = None) -> str:
    sf, q = summary["safety"], summary["quality"]
    k = summary.get("repeat", 1)
    lines = [
        f"# Golden regression — {summary['mode']} — {'PASS' if summary['ok'] else 'FAIL'}",
        "",
        f"- **Safety** (zero failures required): {sf['ran'] - sf['failed']}/{sf['ran']} — "
        f"{'PASS' if sf['ok'] else 'FAIL'}" + (f" — failed: {', '.join(sf['failed_ids'])}" if sf["failed_ids"] else ""),
        f"- **Quality** pass rate: {q['pass_rate'] * 100:.1f}% ({q['passed']}/{q['ran']}; "
        f"threshold {q['threshold'] * 100:.0f}%, baseline {q['baseline']}) — {'PASS' if q['ok'] else 'FAIL'}",
        f"- Overall: {summary['passed']}/{summary['ran']} ran, {summary['skipped']} skipped"
        + (f"; {k} runs per case" if k > 1 else ""),
        "",
        f"| category | pass | fail | skipped | pass@{k} | pass^{k} |",
        "|---|---|---|---|---|---|",
    ]
    for c, v in sorted(summary["by_category"].items()):
        lines.append(
            f"| {c} | {v['pass']} | {v['fail']} | {v['skipped']} | {v.get('pass_at_k', '-')} | {v.get('pass_pow_k', '-')} |"
        )
    if summary.get("judge"):
        lines += ["", "| judge rubric | n | passed | mean score | method | version |", "|---|---|---|---|---|---|"]
        for name, v in summary["judge"].items():
            methods = ", ".join(f"{m}={n}" for m, n in v["methods"].items())
            lines.append(f"| {name} | {v['n']} | {v['passed']} | {v['mean_score']} | {methods} | {v['version']} |")
    lines += ["", "| case | category | result | ms | notes |", "|---|---|---|---|---|"]
    for r in results:
        note = "; ".join(r["failures"])[:300] or r.get("skip_reason", "") or r["description"]
        lines.append(
            f"| {r['id']} | {r['category']} | {r['status'].upper()} | {r['duration_ms']} | {note.replace('|', '/')} |"
        )
    if manifest:
        lines += [
            "",
            "<details><summary>Run manifest</summary>",
            "",
            "```json",
            json.dumps({k: v for k, v in manifest.items() if k != "args"}, ensure_ascii=False, indent=2),
            "```",
            "</details>",
        ]
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None, *, before_cases=None) -> int:
    args = _parse_args(argv)
    ctx = _prepare_env(args.mode)
    _strip_keys(args.mode)
    import app.main  # noqa: F401  (loads .env)

    _strip_keys(args.mode)

    from app.gateway.llm_gateway import has_llm

    if args.mode == "live" and not has_llm():
        print("GOLDEN: --mode live requested but no LLM key is configured", file=sys.stderr)
        return 2

    started = time.time()
    cases = _load_cases(args.cases_dir, args.only)
    raw_cases = {c["id"]: json.loads(json.dumps(c)) for c in cases}
    runner = Runner(args, ctx)
    results: list[dict[str, Any]] = []
    try:
        if before_cases is not None:
            before_cases()
        for case in cases:
            r = _combine_runs([runner.run_case(case, attempt=i) for i in range(args.repeat)])
            results.append(r)
            mark = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP"}[r["status"]]
            reps = f" {r.get('passes', 0)}/{r['attempts']}" if args.repeat > 1 and r.get("attempts") else ""
            print(f"[{mark}] {r['category']:<16} {r['id']:<44} {r['duration_ms']:>8.1f}ms{reps}")
            for msg in r["failures"]:
                print(f"         - {msg}")
        if not args.only:
            for r in runner.global_checks():
                results.append(r)
                print(f"[{'PASS' if r['status'] == 'pass' else 'FAIL'}] {r['category']:<16} {r['id']}")
                for msg in r["failures"]:
                    print(f"         - {msg}")
    finally:
        runner.close()

    summary = _summarize(results, args.mode, args.min_pass_rate, _load_baseline(args.baseline))
    manifest = _manifest(args, runner.run_id, started)
    langfuse_res: Optional[dict[str, Any]] = None
    if args.langfuse:
        langfuse_res = _push_langfuse(results, raw_cases, summary, manifest, args)
        if langfuse_res.get("enabled"):
            print(
                f"LANGFUSE dataset={langfuse_res['dataset']} runs={langfuse_res['runs']} items={langfuse_res['items']} "
                f"run_items={langfuse_res['run_items']} scores={langfuse_res['scores']} errors={len(langfuse_res['errors'])}"
            )
        else:
            print(f"LANGFUSE skipped: {langfuse_res.get('reason')}", file=sys.stderr)
    if args.write_baseline and not args.only:
        base = _load_baseline(args.baseline)
        base.setdefault(args.mode, {})["quality_pass_rate"] = summary["quality"]["pass_rate"]
        Path(args.baseline).write_text(json.dumps(base, indent=2) + "\n", encoding="utf-8")
    md = _markdown(summary, results, manifest)
    if args.report_json:
        Path(args.report_json).write_text(
            json.dumps(
                {"manifest": manifest, "summary": summary, "results": results, "langfuse": langfuse_res},
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    if args.report_md:
        Path(args.report_md).write_text(md, encoding="utf-8")
    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(md)
    print()
    sf, q = summary["safety"], summary["quality"]
    print(
        f"GOLDEN {'PASS' if summary['ok'] else 'FAIL'} mode={summary['mode']} "
        f"pass_rate={summary['pass_rate'] * 100:.1f}% ({summary['passed']}/{summary['ran']}) "
        f"skipped={summary['skipped']}"
    )
    print(
        f"  safety  {'PASS' if sf['ok'] else 'FAIL'} failed={sf['failed']}/{sf['ran']} (must be 0)"
        + (f" {sf['failed_ids']}" if sf["failed_ids"] else "")
    )
    print(
        f"  quality {'PASS' if q['ok'] else 'FAIL'} rate={q['pass_rate'] * 100:.1f}% "
        f"({q['passed']}/{q['ran']}) threshold={q['threshold'] * 100:.0f}% baseline={q['baseline']}"
    )
    for c, v in sorted(summary["by_category"].items()):
        extra = f" pass@{summary['repeat']}={v['pass_at_k']} pass^{summary['repeat']}={v['pass_pow_k']}" if summary["repeat"] > 1 else ""
        print(f"  {c:<18} pass={v['pass']} fail={v['fail']} skipped={v['skipped']}{extra}")
    for name, v in (summary.get("judge") or {}).items():
        print(f"  judge {name:<16} n={v['n']} passed={v['passed']} mean={v['mean_score']} methods={v['methods']}")
    print(f"  manifest git={manifest['git']['sha'][:7] or '-'} cases={manifest['cases']['sha']} prompts={len(manifest['prompt_versions'])}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
