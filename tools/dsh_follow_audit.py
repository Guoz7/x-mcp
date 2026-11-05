"""Audit a DSH-driven follow test against the browser ledger.

Reads the ledger written by the MCP server plus the DSH run folders, then
reports only actions the ledger proves were Following. A verified action must
be `completed`, `changed=true`, `verified=true` and `state=following`.

Attribution comes from the run logs: a batch counts as a DSH run only when its
batch id appears in that run's `agent.jsonl`. The model comes from the run's
`install.patch.yml`.

Usage:
    python tools/dsh_follow_audit.py --state-dir <ledger> --runs <releases/demos>
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import pathlib
import re

BATCH_RE = re.compile(r"follow_batch_[0-9a-f]{8,}")
SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))


def load_runs(root: pathlib.Path) -> tuple[dict[str, set[str]], dict[str, str | None]]:
    batches: dict[str, set[str]] = {}
    models: dict[str, str | None] = {}
    for run in sorted(root.iterdir()):
        if not run.is_dir() or not run.name.startswith("dsh-"):
            continue
        log = run / "agent.jsonl"
        if not log.exists():
            continue
        batches[run.name] = set(BATCH_RE.findall(log.read_text(encoding="utf-8", errors="replace")))
        patch = run / "install.patch.yml"
        model = None
        if patch.exists():
            found = re.search(r"model:\s*([^\s]+)", patch.read_text(encoding="utf-8", errors="replace"))
            model = found.group(1) if found else None
        models[run.name] = model
    return batches, models


def stamp(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--require-model", default="deepseek")
    args = parser.parse_args()

    ledger = json.loads((pathlib.Path(args.state_dir) / "state.json").read_text(encoding="utf-8"))
    run_batches, run_models = load_runs(pathlib.Path(args.runs))

    batch_run: dict[str, str] = {}
    for run, ids in run_batches.items():
        for batch_id in ids:
            batch_run.setdefault(batch_id, run)

    rows = [a for a in ledger["actions"].values() if a.get("batch_id") in batch_run]
    if args.require_model:
        rows = [a for a in rows
                if args.require_model.lower() in (run_models.get(batch_run[a["batch_id"]]) or "").lower()]

    verified, noop, failed, uncertain = [], [], [], []
    for action in rows:
        result = action.get("result") or {}
        if action.get("status") == "uncertain":
            uncertain.append(action)
        elif action.get("status") == "completed":
            if result.get("verified") is True and result.get("state") == "following":
                (verified if result.get("changed") is True else noop).append(action)
            else:
                failed.append(action)

    unique: dict[str, dict] = {}
    duplicates = 0
    for action in verified:
        handle = str((action.get("result") or {}).get("handle")
                     or (action.get("payload") or {}).get("handle") or "").casefold()
        if handle in unique:
            duplicates += 1
            continue
        unique[handle] = action

    def durations(actions):
        out = []
        for action in actions:
            start, end = stamp(action.get("claimed_at")), stamp(action.get("completed_at"))
            if start is not None and end is not None:
                out.append(end - start)
        return sorted(out)

    stored = sorted(a["result"]["elapsed_seconds"] for a in unique.values()
                    if isinstance((a.get("result") or {}).get("elapsed_seconds"), (int, float)))
    uncertain_durations = durations(uncertain)
    visual = [a for a in uncertain
              if "visual_fallback" in json.dumps(a.get("error") or {}, ensure_ascii=False)]
    rate_limited = [a for a in uncertain
                    if ((a.get("error") or {}).get("details") or {}).get("api_response", {}).get("status") == 429]
    mutual = sum(1 for a in unique.values() if (a.get("payload") or {}).get("mutual_verified") is True)

    first = min((stamp(a.get("completed_at") or a.get("created_at")) for a in unique.values()), default=None)
    last = max((stamp(a.get("completed_at") or a.get("created_at")) for a in unique.values()), default=None)

    def pct(values, point):
        return round(values[int(len(values) * point)], 2) if values else None

    summary = {
        "ledger": str(pathlib.Path(args.state_dir) / "state.json"),
        "dsh_runs_scanned": len(run_batches),
        "runs_with_actions": len({batch_run[a["batch_id"]] for a in rows}),
        "runs_with_verified_follows": len({batch_run[a["batch_id"]] for a in unique.values()}),
        "actions_in_dsh_batches": len(rows),
        "verified_following_unique": len(unique),
        "verified_duplicate_rows": duplicates,
        "already_following_noop": len(noop),
        "completed_unverified": len(failed),
        "uncertain": len(uncertain),
        "uncertain_visual_fallback": len(visual),
        "uncertain_rate_limited": len(rate_limited),
        "mutual_proof": mutual,
        "first_verified_shanghai": datetime.datetime.fromtimestamp(first, SHANGHAI).isoformat() if first else None,
        "last_verified_shanghai": datetime.datetime.fromtimestamp(last, SHANGHAI).isoformat() if last else None,
        "elapsed_stored_coverage": [len(stored), len(unique)],
        "elapsed_seconds_median": pct(stored, 0.5),
        "elapsed_seconds_p90": pct(stored, 0.9),
        "uncertain_duration_coverage": [len(uncertain_durations), len(uncertain)],
        "uncertain_seconds_median": pct(uncertain_durations, 0.5),
        "models": dict(collections.Counter(run_models.get(batch_run[a["batch_id"]]) or "unknown"
                                           for a in unique.values())),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
