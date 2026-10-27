"""Synthetic 200-target follow pressure test.

This measures planner, confirmation, durable ledger, and evidence-chain cost.
It deliberately never opens a browser and never contacts X.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time

from browser_bridge import BrowserBridge
from follow_batch import plan_batch


def synthetic_targets(count: int) -> list[dict[str, object]]:
    return [
        {
            "handle": f"mutual{i:03d}",
            "profile_url": f"https://x.com/mutual{i:03d}",
            "mutual_verified": True,
            "state": "follow_back_available",
            "candidate_reason": "synthetic visible Follows you + Follow back",
        }
        for i in range(count)
    ]


def run(target_count: int = 200, chunk_size: int = 20) -> dict[str, object]:
    targets = synthetic_targets(target_count)
    plan_started = time.perf_counter()
    plan = plan_batch(targets, budget_seconds=600, chunk_size=chunk_size)
    plan_seconds = time.perf_counter() - plan_started

    with tempfile.TemporaryDirectory(prefix="x-follow-pressure-") as directory:
        bridge = BrowserBridge(directory, account_binding="synthetic-owner")
        prepare_started = time.perf_counter()
        prepared = bridge.prepare_follow_batch(plan, session_id="pressure")
        prepare_seconds = time.perf_counter() - prepare_started
        queue_started = time.perf_counter()
        queued = bridge.queue_follow_batch(
            prepared["batch_id"], prepared["confirmation_phrase"], session_id="pressure"
        )
        queue_seconds = time.perf_counter() - queue_started

        action_times: list[float] = []
        started = time.perf_counter()
        completed = 0
        while True:
            action = bridge.next_action(
                batch_id=queued["batch_id"], session_id="pressure", lease_seconds=600
            )
            if action is None:
                break
            action_started = time.perf_counter()
            payload = action["payload"]
            result = {
                "handle": payload["handle"],
                "profile_url": payload["profile_url"],
                "verified": True,
                "state": "following",
                "changed": True,
            }
            bridge.complete_action(
                action_id=action["action_id"],
                claim_token=action["claim_token"],
                outcome="succeeded",
                result=result,
                error=None,
                executor_context={
                    "browser_family": "chromium",
                    "os": "synthetic",
                    "host": "x.com",
                    "mode": "browser",
                    "page_kind": "x_profile",
                },
                locator_strategy="dom",
                preconditions_checked=["synthetic mutual proof"],
                postconditions_checked=["synthetic Following after reload"],
                session_id="pressure",
            )
            action_times.append(time.perf_counter() - action_started)
            completed += 1

        elapsed = time.perf_counter() - started
        status = bridge.follow_batch_status(queued["batch_id"], session_id="pressure")
        evidence = bridge.evidence(limit=200)
        ordered = sorted(action_times)
        p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
        return {
            "scope": "synthetic local ledger pressure test; no browser, X network, or model",
            "target_count": target_count,
            "plan": {
                "operation_count": plan["operation_count"],
                "chunk_size": plan["chunk_size"],
                "chunk_count": plan["chunk_count"],
                "seconds": round(plan_seconds, 6),
            },
            "prepare_seconds": round(prepare_seconds, 3),
            "queue_seconds": round(queue_seconds, 3),
            "ledger_execute": {
                "completed": completed,
                "uncertain": 0,
                "seconds": round(elapsed, 3),
                "actions_per_second": round(completed / elapsed, 2) if elapsed else None,
                "mean_action_ms": round(statistics.fmean(action_times) * 1000, 3),
                "p95_action_ms": round(p95 * 1000, 3),
            },
            "status": {
                "state": status["state"],
                "counts": status["counts"],
                "remaining": status["remaining"],
                "changed": status["changed"],
            },
            "evidence": {"valid": evidence["valid"], "count": evidence["count"]},
            "projection": {
                "local_ledger_seconds_for_200": round(elapsed, 3),
                "browser_or_x_not_measured": True,
                "ten_minute_sla_proven": False,
            },
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=int, default=200)
    parser.add_argument("--chunk-size", type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(run(args.targets, args.chunk_size), ensure_ascii=False, indent=2))
