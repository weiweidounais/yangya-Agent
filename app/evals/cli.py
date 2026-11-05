"""Eval CLI.

  python -m app.evals.cli rubrics                         # list rubrics + versions
  python -m app.evals.cli judge --rubric empathy --input "又被拒了" --output "..."
  python -m app.evals.cli push-prompts [--label production] [--dry-run]
  python -m app.evals.cli evaluate-traces [--limit 50] [--hours 24] [--dry-run]

``push-prompts`` publishes every registered system prompt (``personal-agent/<name>``,
label ``sha-<hash>``) and every judge rubric (``judge/<name>``, with ``{{input}}`` /
``{{output}}`` variables, usable as Langfuse evaluator templates). Version numbers
are saved so new generations link to the Langfuse prompt.

``evaluate-traces`` is the external evaluator: it pulls recent traces from Langfuse,
picks rubrics per trace (crisis signal → crisis_handling, chat → empathy), judges
them and posts ``judge.<rubric>`` scores. Traces already scored are skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


def _boot() -> None:
    import app.main  # noqa: F401  registers prompts, loads .env


def cmd_rubrics(_: argparse.Namespace) -> int:
    from app.evals.rubrics import RUBRICS, as_dict

    print(json.dumps([{**as_dict(r), "prompt": r.prompt} for r in RUBRICS.values()], ensure_ascii=False, indent=2))
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    _boot()
    from app.evals.judge import judge

    res = judge(args.rubric, input=args.input, output=args.output, context=args.context, mode=args.mode)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["pass"] else 1


def cmd_push_prompts(args: argparse.Namespace) -> int:
    _boot()
    from app.evals.rubrics import RUBRICS
    from app.observability.langfuse_client import LangfuseClient
    from app.observability.prompts import registry, save_langfuse_link

    items: list[dict[str, Any]] = []
    for name, v in registry().items():
        items.append({"local": name, "name": f"personal-agent/{name}", "prompt": v["text"], "hash": v["hash"], "kind": "system"})
    for r in RUBRICS.values():
        items.append(
            {
                "local": f"judge.{r.name}",
                "name": f"judge/{r.name}",
                "prompt": r.prompt,
                "hash": r.version,
                "kind": "judge",
                "config": {"threshold": r.threshold, "safety": r.safety, "variables": ["input", "output", "context"]},
            }
        )
    if args.dry_run:
        print(json.dumps([{k: v for k, v in i.items() if k != "prompt"} for i in items], ensure_ascii=False, indent=2))
        return 0
    client = LangfuseClient()
    if not client.enabled:
        print(f"langfuse disabled: {client.cfg.reason}", file=sys.stderr)
        return 2
    out = []
    for i in items:
        labels = [f"sha-{i['hash']}"] + ([args.label] if args.label else [])
        res = client.create_prompt(
            name=i["name"],
            prompt=i["prompt"],
            labels=labels,
            config=i.get("config") or {"source": "personal-agent-service", "hash": i["hash"]},
            tags=[i["kind"]],
            commit_message=f"sync {i['hash']}",
        )
        version = res.get("version")
        if version is not None and i["kind"] == "system":
            save_langfuse_link(i["local"], i["hash"], int(version), i["name"])
        out.append({"name": i["name"], "hash": i["hash"], "version": version})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _pick_rubrics(trace: dict[str, Any]) -> list[str]:
    from app.agent.subgraphs.chat import has_crisis_signal

    tags = set(trace.get("tags") or [])
    text = trace.get("input")
    text = text if isinstance(text, str) else json.dumps(text or "", ensure_ascii=False)
    if not trace.get("output"):
        return []
    try:
        if has_crisis_signal(text):
            return ["crisis_handling"]
    except Exception:
        pass
    if "node:chat" in tags:
        return ["empathy"]
    return []


def _as_text(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v or "", ensure_ascii=False)


def evaluate_traces(
    client: Any,
    *,
    limit: int = 50,
    hours: float = 24.0,
    dry_run: bool = False,
    mode: Optional[str] = None,
) -> list[dict[str, Any]]:
    from app.evals.judge import judge

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    listing = client.list_traces(limit=min(100, limit), fromTimestamp=since)
    results = []
    for t in (listing.get("data") or [])[:limit]:
        trace = t if ("input" in t and "output" in t) else client.get_trace(t["id"])
        rubrics = _pick_rubrics(trace)
        have = {s.get("name") for s in trace.get("scores") or [] if isinstance(s, dict)}
        for name in rubrics:
            score_name = f"judge.{name}"
            if score_name in have:
                continue
            res = judge(name, input=_as_text(trace.get("input")), output=_as_text(trace.get("output")), mode=mode)
            results.append({"trace_id": trace["id"], **res})
            if not dry_run:
                client.create_score(
                    trace_id=trace["id"],
                    name=score_name,
                    value=res["score"],
                    comment=res["reason"],
                    metadata={"version": res["version"], "method": res["method"], "model": res["model"], "pass": res["pass"]},
                )
    return results


def cmd_evaluate_traces(args: argparse.Namespace) -> int:
    _boot()
    from app.observability.langfuse_client import LangfuseClient

    client = LangfuseClient()
    if not client.enabled:
        print(f"langfuse disabled: {client.cfg.reason}", file=sys.stderr)
        return 2
    res = evaluate_traces(client, limit=args.limit, hours=args.hours, dry_run=args.dry_run, mode=args.mode)
    print(json.dumps({"scored": len(res), "failed": [r for r in res if not r["pass"]]}, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.evals.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rubrics").set_defaults(fn=cmd_rubrics)
    j = sub.add_parser("judge")
    j.add_argument("--rubric", required=True, choices=("empathy", "crisis_handling"))
    j.add_argument("--input", default="")
    j.add_argument("--output", required=True)
    j.add_argument("--context", default="")
    j.add_argument("--mode", choices=("auto", "llm", "heuristic"), default=None)
    j.set_defaults(fn=cmd_judge)
    p = sub.add_parser("push-prompts")
    p.add_argument("--label", default="")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_push_prompts)
    e = sub.add_parser("evaluate-traces")
    e.add_argument("--limit", type=int, default=50)
    e.add_argument("--hours", type=float, default=24.0)
    e.add_argument("--mode", choices=("auto", "llm", "heuristic"), default=None)
    e.add_argument("--dry-run", action="store_true")
    e.set_defaults(fn=cmd_evaluate_traces)
    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
