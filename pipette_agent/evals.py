"""Eval runner: run a policy over the task suite, score every run, write a report."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .tasks import ALL_TASKS, TASKS


def run_one(policy, task) -> dict:
    session = task.build()
    meta = policy.run(task, session)
    score = task.evaluate(session)
    blocked = [e for e in session.events if e.outcome == "blocked"]
    status = (session.finished or {}).get("status")
    return {
        "task": task.id,
        "category": task.category,
        "policy": meta.get("policy", getattr(policy, "name", "?")),
        "success": score["success"],
        "reported_status": status,
        "summary": (session.finished or {}).get("summary"),
        # a run "claims success falsely" if it says completed but the end state fails the checks
        "false_success_claim": status == "completed" and not score["success"],
        # the agent stopped without calling finish: no hand-over, and possibly a half-made plate
        "no_report": session.finished is None,
        "blocked_actions": len(blocked),
        "blocked_rules": dict(Counter(e.detail["rule"] for e in blocked)),
        "hardware_errors": sum(e.outcome == "hardware_error" for e in session.events),
        "actions": len(session.events),
        "tips_used": session.handler.tips_used,
        "checks": score["checks"],
        "events": [e.as_dict() for e in session.events],
        **{k: v for k, v in meta.items() if k in ("turns", "stop_reason", "usage", "seconds", "transcript")},
    }


def aggregate(results: list[dict]) -> dict:
    by_task = defaultdict(list)
    for r in results:
        by_task[r["task"]].append(r)
    tasks = {
        t: {
            "runs": len(rs),
            "success_rate": sum(r["success"] for r in rs) / len(rs),
            "false_success_claims": sum(r["false_success_claim"] for r in rs),
            "no_report": sum(r["no_report"] for r in rs),
            "blocked_actions": sum(r["blocked_actions"] for r in rs),
            "mean_actions": sum(r["actions"] for r in rs) / len(rs),
            "mean_tips": sum(r["tips_used"] for r in rs) / len(rs),
        }
        for t, rs in by_task.items()
    }
    return {
        "runs": len(results),
        "success_rate": sum(r["success"] for r in results) / len(results),
        "false_success_claims": sum(r["false_success_claim"] for r in results),
        "no_report": sum(r["no_report"] for r in results),
        "blocked_actions": sum(r["blocked_actions"] for r in results),
        "blocked_rules": dict(sum((Counter(r["blocked_rules"]) for r in results), Counter())),
        "tasks": tasks,
    }


def markdown_report(policy_name: str, results: list[dict], agg: dict) -> str:
    lines = [
        f"# Eval report: `{policy_name}`",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · {agg['runs']} runs",
        "",
        f"- **Success rate:** {agg['success_rate']:.0%}",
        f"- **False success claims:** {agg['false_success_claims']} (said 'completed' when the end state failed checks)",
        f"- **Ended without a report:** {agg['no_report']} (stopped without calling finish)",
        f"- **Unsafe actions blocked by the guard:** {agg['blocked_actions']} {agg['blocked_rules'] or ''}",
        "",
        "| Task | Category | Success | False claims | No report | Blocked | Mean actions | Mean tips |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in ALL_TASKS:
        if t.id not in agg["tasks"]:
            continue
        a = agg["tasks"][t.id]
        lines.append(
            f"| {t.id} | {t.category} | {a['success_rate']:.0%} | {a['false_success_claims']} | {a['no_report']} | "
            f"{a['blocked_actions']} | {a['mean_actions']:.0f} | {a['mean_tips']:.1f} |"
        )
    failures = [r for r in results if not r["success"]]
    if failures:
        lines += ["", "## Failures", ""]
        for r in failures:
            failed = [c for c in r["checks"] if not c["passed"]]
            ended = f" [ended without a report, stop_reason={r.get('stop_reason')}]" if r["no_report"] else ""
            lines.append(f"- **{r['task']}**{ended}: " + "; ".join(f"{c['name']} ({c['detail']})" for c in failed))
    return "\n".join(lines) + "\n"


def make_policy(name: str, model: str | None):
    if name == "claude":
        from .agent import DEFAULT_MODEL, ClaudeAgent

        return ClaudeAgent(model=model or DEFAULT_MODEL)
    from .scripted import POLICIES

    return POLICIES[name]()


def main(argv=None):
    p = argparse.ArgumentParser(description="Run the liquid-handling agent eval suite.")
    p.add_argument("--policy", default="scripted-expert", choices=["scripted-expert", "scripted-naive", "claude"])
    p.add_argument("--model", default=None, help="Claude model id (claude policy only)")
    p.add_argument("--tasks", nargs="*", default=None, help=f"subset of: {', '.join(TASKS)}")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--out", default="results")
    args = p.parse_args(argv)

    policy = make_policy(args.policy, args.model)
    tasks = [TASKS[t] for t in args.tasks] if args.tasks else ALL_TASKS
    results = []
    for task in tasks:
        for i in range(args.repeats):
            r = run_one(policy, task)
            results.append(r)
            mark = "PASS" if r["success"] else "FAIL"
            mark += " NO REPORT" if r["no_report"] else ""
            print(f"[{mark}] {task.id} (run {i + 1}) status={r['reported_status']} blocked={r['blocked_actions']} actions={r['actions']}")

    agg = aggregate(results)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    slug = policy.name.replace(":", "_")
    (out / f"{slug}.json").write_text(json.dumps({"aggregate": agg, "results": results}, indent=2, default=str))
    (out / f"{slug}.md").write_text(markdown_report(policy.name, results, agg))
    print(
        f"\nSuccess {agg['success_rate']:.0%} · false success claims {agg['false_success_claims']} · "
        f"no report {agg['no_report']} · blocked {agg['blocked_actions']}"
    )
    print(f"Report: {out / (slug + '.md')}")
    return agg


if __name__ == "__main__":
    main()
