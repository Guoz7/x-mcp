"""Bounded autopilot authorization, micro-wave scheduling, and 7-day ops summary.

Instead of asking the owner to type a confirmation phrase for every 3-5 action
wave, the owner authorizes one bounded window (duration, active local hours,
per-wave cap, and daily goals). Each wave still runs through the circuit
breaker, single-use action leases, and page postcondition verification.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from account_orchestrator import (
    ALLOWED_STRATEGIES,
    CONCRETE_STRATEGIES,
    STRATEGY_SPECS,
    list_orchestration_strategies,
)
from daily_action_budget import LOCAL_TIMEZONE, _local_date, _parse_epoch
from ledger_lock import LedgerLock


class AutopilotError(Exception):
    """Structured error safe to return through MCP and the web UI."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class AutopilotStore:
    """Durable JSON store for bounded autopilot windows and wave execution logs."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "autopilot.json"
        self.lock = LedgerLock(self.state_dir / ".autopilot.lock")

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "prepared": {}, "active_window": None, "waves_log": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("prepared", {})
                data.setdefault("active_window", None)
                data.setdefault("waves_log", [])
                return data
        except ValueError:
            pass
        return {"version": 1, "prepared": {}, "active_window": None, "waves_log": []}

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def prepare_window(
        self,
        *,
        days: int = 7,
        active_hours: tuple[int, int] | list[int] = (9, 23),
        max_per_wave: int = 9,
        min_wave_interval_minutes: int = 18,
        daily_goals: dict[str, int] | None = None,
        persona_topics: list[str] | None = None,
        exclude_keywords: list[str] | None = None,
        search_query: str | None = None,
        interaction_strategy: str = "reciprocal_first",
        expires_in_seconds: int = 86400,
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        now = float(now_epoch) if now_epoch is not None else time.time()
        if not 1 <= int(days) <= 30:
            raise AutopilotError("INVALID_ARGUMENT", "days must be between 1 and 30.")
        if not isinstance(active_hours, (list, tuple)) or len(active_hours) != 2:
            raise AutopilotError("INVALID_ARGUMENT", "active_hours must be [start_hour, end_hour].")
        start_h, end_h = int(active_hours[0]), int(active_hours[1])
        if not (0 <= start_h < end_h <= 24):
            raise AutopilotError("INVALID_ARGUMENT", "active_hours must satisfy 0 <= start < end <= 24.")
        if not 1 <= int(max_per_wave) <= 15:
            raise AutopilotError("INVALID_ARGUMENT", "max_per_wave must be between 1 and 15.")
        if not 10 <= int(min_wave_interval_minutes) <= 720:
            raise AutopilotError("INVALID_ARGUMENT", "min_wave_interval_minutes must be between 10 and 720.")
        strategy = str(interaction_strategy or "reciprocal_first").strip()
        if strategy not in ALLOWED_STRATEGIES:
            raise AutopilotError(
                "INVALID_ARGUMENT",
                f"interaction_strategy must be one of {', '.join(sorted(ALLOWED_STRATEGIES))}.",
            )
        ttl = max(60, min(604800, int(expires_in_seconds)))

        goals = daily_goals or {
            "publish_post": 2,
            "like_post": 150,
            "follow_profile": 40,
            "repost_post": 2,
            "reply_post": 240,
        }
        window_id = "ap_" + secrets.token_hex(6)
        phrase = "AUTOPILOT " + secrets.token_hex(3).upper()
        record = {
            "window_id": window_id,
            "confirmation_phrase": phrase,
            "days": int(days),
            "active_hours": [start_h, end_h],
            "max_per_wave": int(max_per_wave),
            "min_wave_interval_minutes": int(min_wave_interval_minutes),
            "daily_goals": goals,
            "persona_topics": [str(k).strip() for k in (persona_topics or []) if str(k).strip()][:20],
            "exclude_keywords": [str(k).strip() for k in (exclude_keywords or []) if str(k).strip()][:20],
            "search_query": str(search_query).strip()[:120] if search_query else None,
            "interaction_strategy": strategy,
            "created_at": _iso(now),
            "expires_epoch": now + ttl,
        }
        with self.lock:
            data = self._read()
            data["prepared"][window_id] = record
            self._write(data)
        return {
            "window_id": window_id,
            "confirmation_phrase": phrase,
            "days": int(days),
            "active_hours": [start_h, end_h],
            "max_per_wave": int(max_per_wave),
            "min_wave_interval_minutes": int(min_wave_interval_minutes),
            "daily_goals": goals,
            "interaction_strategy": strategy,
            "expires_at": _iso(record["expires_epoch"]),
        }

    def activate_window(
        self,
        window_id: str,
        confirmation: str,
        *,
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        now = float(now_epoch) if now_epoch is not None else time.time()
        with self.lock:
            data = self._read()
            prep = data["prepared"].get(window_id)
            if not isinstance(prep, dict):
                raise AutopilotError("WINDOW_NOT_FOUND", "The prepared autopilot window does not exist.")
            if now > float(prep["expires_epoch"]):
                raise AutopilotError("WINDOW_EXPIRED", "The autopilot confirmation expired.")
            if confirmation != prep["confirmation_phrase"]:
                raise AutopilotError("CONFIRMATION_MISMATCH", "The confirmation phrase does not match.")

            end_epoch = now + int(prep["days"]) * 86400
            active = {
                "window_id": window_id,
                "enabled": True,
                "activated_at": _iso(now),
                "expires_at": _iso(end_epoch),
                "expires_epoch": end_epoch,
                "active_hours": prep["active_hours"],
                "max_per_wave": prep["max_per_wave"],
                "min_wave_interval_minutes": prep["min_wave_interval_minutes"],
                "daily_goals": prep["daily_goals"],
                "persona_topics": prep.get("persona_topics") or [],
                "exclude_keywords": prep.get("exclude_keywords") or [],
                "search_query": prep.get("search_query"),
                "interaction_strategy": prep.get("interaction_strategy") or "reciprocal_first",
                "last_wave_epoch": None,
                "last_notif_sync_epoch": None,
                "stop_reason": None,
            }
            data["active_window"] = active
            data["prepared"].pop(window_id, None)
            self._write(data)
            return active

    def mark_notif_sync(self, *, now_epoch: float | None = None) -> None:
        now = float(now_epoch) if now_epoch is not None else time.time()
        with self.lock:
            data = self._read()
            active = data.get("active_window")
            if isinstance(active, dict):
                active["last_notif_sync_epoch"] = now
                self._write(data)

    def stop_window(self, *, reason: str = "manual_stop", now_epoch: float | None = None) -> dict[str, Any]:
        now = float(now_epoch) if now_epoch is not None else time.time()
        with self.lock:
            data = self._read()
            active = data.get("active_window")
            if isinstance(active, dict):
                active["enabled"] = False
                active["stopped_at"] = _iso(now)
                active["stop_reason"] = reason
                self._write(data)
                return dict(active)
            return {"enabled": False, "stop_reason": reason}

    def set_strategy(self, interaction_strategy: str, *, now_epoch: float | None = None) -> dict[str, Any]:
        """Switch the active autopilot window's orchestration strategy cleanly inside the same owner window."""
        now = float(now_epoch) if now_epoch is not None else time.time()
        strategy = str(interaction_strategy or "").strip()
        if strategy not in ALLOWED_STRATEGIES:
            raise AutopilotError(
                "INVALID_ARGUMENT",
                f"interaction_strategy must be one of {', '.join(sorted(ALLOWED_STRATEGIES))}.",
            )
        with self.lock:
            data = self._read()
            active = data.get("active_window")
            if not isinstance(active, dict) or not active.get("enabled") or now >= float(active.get("expires_epoch", 0)):
                raise AutopilotError("AUTOPILOT_NOT_ENABLED", "Activate an autopilot window before switching strategy.")
            prev = active.get("interaction_strategy") or "reciprocal_first"
            active["interaction_strategy"] = strategy
            active["strategy_updated_at"] = _iso(now)
            self._write(data)
            return {"previous_strategy": prev, "interaction_strategy": strategy, "active_window": dict(active)}

    def record_wave(self, summary: dict[str, Any], *, now_epoch: float | None = None) -> None:
        import random

        now = float(now_epoch) if now_epoch is not None else time.time()
        rng = random.SystemRandom()
        with self.lock:
            data = self._read()
            active = data.get("active_window")
            if isinstance(active, dict):
                active["last_wave_epoch"] = now
                base_sec = max(600, int(active.get("min_wave_interval_minutes", 90)) * 60)
                # Human-like irregular spacing: normal jitter (0.65x..1.55x) with occasional longer break (1.6x..2.4x).
                mult = rng.uniform(1.6, 2.4) if rng.random() < 0.20 else rng.uniform(0.65, 1.55)
                active["next_wave_epoch"] = now + base_sec * mult
                base_max = max(1, int(active.get("max_per_wave", 4)))
                low = max(1, base_max - 2)
                high = min(15, base_max + 2)
                active["next_wave_limit"] = rng.randint(low, high)
            log = list(data.get("waves_log") or [])
            log.append({"at": _iso(now), "at_epoch": now, **summary})
            data["waves_log"] = log[-200:]
            self._write(data)

    def status(self, *, now_epoch: float | None = None) -> dict[str, Any]:
        now = float(now_epoch) if now_epoch is not None else time.time()
        with self.lock:
            data = self._read()
            active = data.get("active_window")
            waves_log = list(data.get("waves_log") or [])
            prepared_raw = data.get("prepared") or {}
        pending_prepared = [
            {
                "window_id": wid,
                "confirmation_phrase": rec.get("confirmation_phrase"),
                "days": rec.get("days"),
                "active_hours": rec.get("active_hours"),
                "max_per_wave": rec.get("max_per_wave"),
                "min_wave_interval_minutes": rec.get("min_wave_interval_minutes"),
                "daily_goals": rec.get("daily_goals"),
                "persona_topics": rec.get("persona_topics") or [],
                "interaction_strategy": rec.get("interaction_strategy") or "reciprocal_first",
                "expires_at": _iso(float(rec["expires_epoch"])),
            }
            for wid, rec in prepared_raw.items()
            if isinstance(rec, dict) and float(rec.get("expires_epoch", 0)) > now
        ]
        if not isinstance(active, dict) or not active.get("enabled"):
            return {
                "enabled": False,
                "active_window": active,
                "in_active_hours": False,
                "wave_due": False,
                "recent_waves": waves_log[-10:],
                "waves_history": waves_log[-100:],
                "pending_prepared": pending_prepared,
            }
        if now >= float(active.get("expires_epoch", 0)):
            return {
                "enabled": False,
                "active_window": {**active, "enabled": False, "stop_reason": "window_expired"},
                "in_active_hours": False,
                "wave_due": False,
                "recent_waves": waves_log[-10:],
                "waves_history": waves_log[-100:],
                "pending_prepared": pending_prepared,
            }
        local_tz = datetime.fromtimestamp(now).astimezone().tzinfo or LOCAL_TIMEZONE
        local_hour = datetime.fromtimestamp(now, local_tz).hour
        start_h, end_h = active["active_hours"]
        in_hours = start_h <= local_hour < end_h
        last_wave = active.get("last_wave_epoch")
        min_interval = int(active.get("min_wave_interval_minutes", 90)) * 60
        planned_next = active.get("next_wave_epoch")
        if planned_next is not None:
            next_wave = float(planned_next)
        elif last_wave is not None:
            next_wave = float(last_wave) + min_interval
        else:
            next_wave = now
        interval_ok = last_wave is None or now >= next_wave
        return {
            "enabled": True,
            "active_window": active,
            "in_active_hours": in_hours,
            "wave_due": in_hours and interval_ok,
            "next_wave_epoch": next_wave,
            "recent_waves": waves_log[-10:],
            "waves_history": waves_log[-100:],
            "pending_prepared": pending_prepared,
        }


_EXPLORE_ORDER: tuple[str, ...] = (
    "reciprocal_first",
    "niche_sniper_bundle",
    "thread_reply_magnet",
    "reply_then_follow",
    "balanced",
)


def _int_or_none(val: Any) -> int | None:
    if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
        return int(val)
    return None


def _collect_follower_snapshots(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect deduplicated chronological follower-count observations from real snapshots."""
    points: list[dict[str, Any]] = []
    raw_snaps = ledger.get("follower_snapshots")
    if isinstance(raw_snaps, list):
        for item in raw_snaps:
            if not isinstance(item, dict):
                continue
            fc = _int_or_none(item.get("followers_count"))
            ep = item.get("at_epoch")
            ep_val = float(ep) if isinstance(ep, (int, float)) and not isinstance(ep, bool) else _parse_epoch(item.get("at"))
            if fc is None or ep_val is None:
                continue
            points.append({
                "at": str(item.get("at") or _iso(ep_val)),
                "at_epoch": ep_val,
                "date": str(item.get("date") or _local_date(ep_val).isoformat()),
                "followers_count": fc,
                "following_count": _int_or_none(item.get("following_count")),
                "posts_count": _int_or_none(item.get("posts_count")),
                "strategy": item.get("strategy"),
                "source": str(item.get("source") or "snapshot"),
            })

    actions = ledger.get("actions")
    for action in (actions.values() if isinstance(actions, dict) else ()):
        if not isinstance(action, dict) or action.get("status") != "completed":
            continue
        if action.get("type") not in {"observe_account", "owner_read_account"}:
            continue
        ep_val = _parse_epoch(action.get("completed_at")) or _parse_epoch(action.get("created_at"))
        res = action.get("result") or {}
        snap = res.get("snapshot") if isinstance(res.get("snapshot"), dict) else res
        if not isinstance(snap, dict) or ep_val is None:
            continue
        fc = _int_or_none(snap.get("followers_count"))
        if fc is None:
            continue
        if any(abs(p["at_epoch"] - ep_val) < 60.0 and p["followers_count"] == fc for p in points):
            continue
        points.append({
            "at": str(action.get("completed_at") or _iso(ep_val)),
            "at_epoch": ep_val,
            "date": _local_date(ep_val).isoformat(),
            "followers_count": fc,
            "following_count": _int_or_none(snap.get("following_count")),
            "posts_count": _int_or_none(snap.get("posts_count")),
            "strategy": None,
            "source": str(action.get("type") or "observe_account"),
        })

    acct = ledger.get("account")
    if isinstance(acct, dict):
        fc = _int_or_none(acct.get("followers_count"))
        ep_val = _parse_epoch(acct.get("observed_at"))
        if fc is not None and ep_val is not None:
            if not any(abs(p["at_epoch"] - ep_val) < 60.0 and p["followers_count"] == fc for p in points):
                points.append({
                    "at": str(acct.get("observed_at") or _iso(ep_val)),
                    "at_epoch": ep_val,
                    "date": _local_date(ep_val).isoformat(),
                    "followers_count": fc,
                    "following_count": _int_or_none(acct.get("following_count")),
                    "posts_count": _int_or_none(acct.get("posts_count")),
                    "strategy": None,
                    "source": "account_snapshot",
                })

    points.sort(key=lambda item: item["at_epoch"])
    return points


def _extract_wave_records(ledger: dict[str, Any], autopilot_status: dict[str, Any]) -> list[dict[str, Any]]:
    """Build unified chronological wave records enriched from ledger batches when available."""
    raw_waves = list(autopilot_status.get("waves_history") or autopilot_status.get("recent_waves") or [])
    batches = ledger.get("batches") if isinstance(ledger.get("batches"), dict) else {}
    actions = ledger.get("actions") if isinstance(ledger.get("actions"), dict) else {}
    mutual_map = ledger.get("mutual_followers") if isinstance(ledger.get("mutual_followers"), dict) else {}
    mutual_handles = {str(k).strip().lstrip("@").casefold() for k in mutual_map.keys() if str(k).strip()}

    records: list[dict[str, Any]] = []
    seen_batch_ids: set[str] = set()

    def _enrich(item: dict[str, Any], *, from_batch: bool = False) -> dict[str, Any] | None:
        raw_strat = str(item.get("interaction_strategy") or "").strip()
        if raw_strat == "auto_fastest":
            raw_strat = str(item.get("resolved_strategy") or "reciprocal_first")
        if raw_strat not in CONCRETE_STRATEGIES:
            raw_strat = "reciprocal_first"
        ep_val = item.get("at_epoch")
        ep = float(ep_val) if isinstance(ep_val, (int, float)) and not isinstance(ep_val, bool) else _parse_epoch(item.get("at") or item.get("created_at"))
        if ep is None:
            return None
        bid = str(item.get("batch_id") or "")
        if bid:
            seen_batch_ids.add(bid)

        counts = dict(item.get("action_counts") or {})
        uncertain = int(item.get("uncertain_count") or 0)
        failed = int(item.get("failed_count") or 0)
        mutual_hits = int(item.get("mutual_hits") or 0)
        batch_record = batches.get(bid) if bid and isinstance(batches.get(bid), dict) else None
        if batch_record and not counts:
            for aid in batch_record.get("action_ids") or ():
                act = actions.get(aid)
                if not isinstance(act, dict):
                    continue
                st = act.get("status")
                atype = str(act.get("type") or "")
                res = act.get("result") or {}
                pay = act.get("payload") or {}
                if st == "uncertain":
                    uncertain += 1
                elif st == "failed":
                    failed += 1
                elif st == "completed" and res.get("verified") is True:
                    counts[atype] = counts.get(atype, 0) + 1
                    if atype == "follow_profile":
                        h = str(res.get("handle") or pay.get("handle") or "").strip().lstrip("@").casefold()
                        if res.get("mutual_verified") is True or (h and h in mutual_handles):
                            mutual_hits += 1

        verified_changed = item.get("verified_changed")
        if verified_changed is None:
            verified_changed = sum(int(v) for v in counts.values())
        processed = item.get("processed")
        if processed is None:
            processed = int(verified_changed) + uncertain + failed
        return {
            "at": str(item.get("at") or item.get("created_at") or _iso(ep)),
            "at_epoch": ep,
            "date": _local_date(ep).isoformat(),
            "batch_id": bid or None,
            "strategy": raw_strat,
            "strategy_requested": str(item.get("strategy_requested") or raw_strat),
            "processed": int(processed),
            "verified_changed": int(verified_changed),
            "uncertain_count": uncertain,
            "failed_count": failed,
            "action_counts": counts,
            "mutual_hits": mutual_hits,
            "stop_reason": item.get("stop_reason"),
            "followers_before": _int_or_none(item.get("followers_before")),
            "followers_after": _int_or_none(item.get("followers_after")),
            "known_inbound_before": _int_or_none(item.get("known_inbound_before")),
            "known_inbound_after": _int_or_none(item.get("known_inbound_after")),
            "from_batch": from_batch,
        }

    for w in raw_waves:
        if isinstance(w, dict):
            rec = _enrich(w, from_batch=False)
            if rec is not None:
                records.append(rec)

    for bid, b in batches.items():
        if bid in seen_batch_ids or not isinstance(b, dict):
            continue
        if b.get("kind") != "daily_orchestration" or not b.get("interaction_strategy"):
            continue
        if not (b.get("action_ids") or ()):
            continue
        rec = _enrich({"batch_id": bid, **b}, from_batch=True)
        if rec is not None:
            records.append(rec)

    records.sort(key=lambda r: r["at_epoch"])
    return records


def _attribute_growth_and_inbound(
    ledger: dict[str, Any],
    wave_records: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    *,
    now_epoch: float,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Attribute inbound followers and observed follower deltas across concrete strategies cleanly."""
    mutual_map = ledger.get("mutual_followers") if isinstance(ledger.get("mutual_followers"), dict) else {}
    unfollowed_handles: set[str] = set()
    actions = ledger.get("actions")
    for act in (actions.values() if isinstance(actions, dict) else ()):
        if isinstance(act, dict) and act.get("type") == "unfollow_profile" and act.get("status") == "completed":
            res = act.get("result") or {}
            pay = act.get("payload") or {}
            if res.get("state") == "unfollowed":
                h = str(res.get("handle") or pay.get("handle") or "").strip().lstrip("@").casefold()
                if h:
                    unfollowed_handles.add(h)

    inbound_events: list[dict[str, Any]] = []
    for raw_k, raw_v in mutual_map.items():
        if not isinstance(raw_v, dict):
            continue
        handle = str(raw_v.get("handle") or raw_k).strip().lstrip("@")
        if not handle:
            continue
        first_ep = _parse_epoch(raw_v.get("verified_at")) or _parse_epoch(raw_v.get("last_seen_at"))
        last_ep = _parse_epoch(raw_v.get("last_seen_at")) or first_ep
        if first_ep is None:
            continue
        explicit_strat = str(raw_v.get("strategy") or "").strip()
        if explicit_strat not in CONCRETE_STRATEGIES:
            explicit_strat = ""
        # Sticky retention heuristic: known inbound follower surviving >= 24h without churn/unfollow.
        sticky = (handle.casefold() not in unfollowed_handles) and (
            (now_epoch - first_ep >= 86400.0) or ((last_ep or first_ep) - first_ep >= 43200.0)
        )
        inbound_events.append({
            "handle": handle,
            "at_epoch": first_ep,
            "date": _local_date(first_ep).isoformat(),
            "strategy": explicit_strat or None,
            "sticky": sticky,
            "source": str(raw_v.get("source") or "followers_sync"),
        })

    stats: dict[str, dict[str, Any]] = {
        s: {
            "strategy": s,
            "label": STRATEGY_SPECS[s]["label"],
            "short_goal": STRATEGY_SPECS[s]["short_goal"],
            "stickiness_tier": STRATEGY_SPECS[s]["stickiness_tier"],
            "waves_run": 0,
            "processed": 0,
            "verified_actions": 0,
            "uncertain_count": 0,
            "failed_count": 0,
            "refusal_stops": 0,
            "follows": 0,
            "replies": 0,
            "likes": 0,
            "reposts": 0,
            "posts": 0,
            "mutual_verified": 0,
            "inbound_attributed": 0.0,
            "sticky_attributed": 0.0,
            "follower_delta": 0.0,
            "follower_windows": 0,
        }
        for s in CONCRETE_STRATEGIES
    }

    for rec in wave_records:
        st = stats[rec["strategy"]]
        st["waves_run"] += 1
        st["processed"] += rec["processed"]
        st["verified_actions"] += rec["verified_changed"]
        st["uncertain_count"] += rec["uncertain_count"]
        st["failed_count"] += rec["failed_count"]
        if str(rec.get("stop_reason") or "").startswith("REFUSAL_CIRCUIT_BREAKER"):
            st["refusal_stops"] += 1
        ac = rec["action_counts"]
        st["follows"] += int(ac.get("follow_profile", 0))
        st["replies"] += int(ac.get("reply_post", 0))
        st["likes"] += int(ac.get("like_post", 0))
        st["reposts"] += int(ac.get("repost_post", 0))
        st["posts"] += int(ac.get("publish_post", 0))
        st["mutual_verified"] += int(rec.get("mutual_hits", 0))

        # Direct pre/post snapshot delta on a wave when both were observed.
        fb, fa = rec.get("followers_before"), rec.get("followers_after")
        if isinstance(fb, int) and isinstance(fa, int) and fa != fb:
            st["follower_delta"] += float(fa - fb)
            st["follower_windows"] += 1

    # Also attribute snapshot-to-snapshot follower_delta across waves executed inside that interval
    # when waves themselves didn't record distinct pre/post counts.
    for idx in range(1, len(snapshots)):
        prev_s, cur_s = snapshots[idx - 1], snapshots[idx]
        if cur_s["at_epoch"] - prev_s["at_epoch"] < 60.0:
            continue
        delta = cur_s["followers_count"] - prev_s["followers_count"]
        window_waves = [
            w for w in wave_records
            if prev_s["at_epoch"] - 300.0 <= w["at_epoch"] <= cur_s["at_epoch"]
            and not (isinstance(w.get("followers_before"), int) and isinstance(w.get("followers_after"), int) and w["followers_before"] != w["followers_after"])
        ]
        if not window_waves:
            continue
        total_weight = sum(max(1, w["verified_changed"]) for w in window_waves)
        for w in window_waves:
            share = max(1, w["verified_changed"]) / total_weight
            st = stats[w["strategy"]]
            st["follower_delta"] += delta * share
            st["follower_windows"] += 1

    # Attribute inbound followers either directly (explicit strategy tag) or across waves in preceding 24h.
    for ev in inbound_events:
        if ev["strategy"] and ev["strategy"] in stats:
            st = stats[ev["strategy"]]
            st["inbound_attributed"] += 1.0
            if ev["sticky"]:
                st["sticky_attributed"] += 1.0
            continue
        recent_waves = [
            w for w in wave_records
            if 0.0 <= (ev["at_epoch"] - w["at_epoch"]) <= 86400.0
        ]
        if not recent_waves:
            continue
        total_w = sum(max(1, w["verified_changed"]) for w in recent_waves)
        for w in recent_waves:
            share = max(1, w["verified_changed"]) / total_w
            st = stats[w["strategy"]]
            st["inbound_attributed"] += share
            if ev["sticky"]:
                st["sticky_attributed"] += share

    return stats, inbound_events


def evaluate_strategy_performance(
    ledger: dict[str, Any],
    autopilot_status: dict[str, Any] | None = None,
    *,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Compare orchestration strategies on follower growth speed, conversion, stickiness & safety."""
    now = float(now_epoch) if now_epoch is not None else time.time()
    ap = autopilot_status or {}
    snapshots = _collect_follower_snapshots(ledger)
    wave_records = _extract_wave_records(ledger, ap)
    stats, _ = _attribute_growth_and_inbound(ledger, wave_records, snapshots, now_epoch=now)

    rows: list[dict[str, Any]] = []
    for s in CONCRETE_STRATEGIES:
        st = stats[s]
        waves_run = st["waves_run"]
        attempts = st["verified_actions"] + st["uncertain_count"] + st["failed_count"]
        uncertain_rate = round(st["uncertain_count"] / attempts, 3) if attempts else 0.0
        follow_back_rate = round(st["mutual_verified"] / st["follows"], 3) if st["follows"] else 0.0
        inbound_attr = round(st["inbound_attributed"], 2)
        inbound_per_wave = round(st["inbound_attributed"] / waves_run, 2) if waves_run else 0.0
        sticky_retention = round(st["sticky_attributed"] / st["inbound_attributed"], 3) if st["inbound_attributed"] > 0 else 0.0
        replies_per_wave = (st["replies"] / waves_run) if waves_run else 0.0
        observed_delta = round(st["follower_delta"], 2) if st["follower_windows"] > 0 else None
        delta_per_wave = round(st["follower_delta"] / st["follower_windows"], 2) if st["follower_windows"] > 0 else None

        if waves_run == 0:
            sticky_score = None
        else:
            raw_score = (
                max(-20.0, (delta_per_wave or 0.0) * 14.0)
                + inbound_per_wave * 18.0
                + follow_back_rate * 40.0
                + sticky_retention * 18.0
                + min(12.0, replies_per_wave * 3.0)
                - (uncertain_rate * 35.0)
                - (st["refusal_stops"] * 20.0)
            )
            sticky_score = round(max(0.0, raw_score), 1)

        rows.append({
            "strategy": s,
            "label": st["label"],
            "short_goal": st["short_goal"],
            "stickiness_tier": st["stickiness_tier"],
            "waves_run": waves_run,
            "verified_actions": st["verified_actions"],
            "follows": st["follows"],
            "replies": st["replies"],
            "likes": st["likes"],
            "posts": st["posts"],
            "mutual_verified": st["mutual_verified"],
            "follow_back_rate": follow_back_rate,
            "inbound_attributed": inbound_attr,
            "inbound_per_wave": inbound_per_wave,
            "sticky_retention_rate": sticky_retention,
            "observed_follower_delta": observed_delta,
            "follower_delta_per_wave": delta_per_wave,
            "uncertain_rate": uncertain_rate,
            "sticky_score": sticky_score,
        })

    tested = [r for r in rows if r["waves_run"] > 0]
    rows.sort(
        key=lambda r: (
            1 if r["waves_run"] > 0 else 0,
            float(r["sticky_score"] if r["sticky_score"] is not None else -1.0),
            float(r["inbound_per_wave"]),
            r["verified_actions"],
        ),
        reverse=True,
    )

    with_signal = [
        r for r in tested
        if r["waves_run"] >= 2 and (
            (r["observed_follower_delta"] is not None) or r["inbound_attributed"] > 0 or r["follows"] > 0
        )
    ]
    if len(with_signal) >= 2 and len(wave_records) >= 4:
        winner = rows[0]
        confidence = "measured"
        recommended = winner["strategy"]
        reason = (
            f"实测 {len(wave_records)} 轮对比中 `{winner['label']}` 粘性增速评分最高 ({winner['sticky_score']})"
        )
    elif tested:
        winner = rows[0]
        confidence = "preliminary"
        recommended = winner["strategy"]
        reason = f"已采集 {len(wave_records)} 轮初步数据，暂以 `{winner['label']}` 领先，建议继续轮换对比"
    else:
        confidence = "cold_start"
        recommended = _EXPLORE_ORDER[0]
        reason = "尚无策略波次记录，建议启用智能择优轮换积累真实增粉数据"

    return {
        "leaderboard": rows,
        "tested_strategies": len(tested),
        "total_waves": len(wave_records),
        "fastest_strategy": {
            "recommended_strategy": recommended,
            "recommended_label": STRATEGY_SPECS[recommended]["label"],
            "confidence": confidence,
            "reason": reason,
        },
    }


def select_best_growth_strategy(
    ledger: dict[str, Any],
    *,
    autopilot_status: dict[str, Any] | None = None,
    wave_index: int = 0,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Select the concrete strategy for `auto_fastest` with measured exploitation + cold-start exploration."""
    perf = evaluate_strategy_performance(ledger, autopilot_status=autopilot_status, now_epoch=now_epoch)
    fastest = perf["fastest_strategy"]
    rows_by_strat = {r["strategy"]: r for r in perf["leaderboard"]}
    idx = abs(int(wave_index))

    if fastest["confidence"] != "measured":
        least_waves = min(rows_by_strat[s]["waves_run"] for s in _EXPLORE_ORDER)
        candidates = [s for s in _EXPLORE_ORDER if rows_by_strat[s]["waves_run"] == least_waves]
        chosen = candidates[idx % len(candidates)]
        return {
            "recommended_strategy": chosen,
            "recommended_label": STRATEGY_SPECS[chosen]["label"],
            "selection_mode": "explore_rotation",
            "confidence": fastest["confidence"],
            "reason": f"explore_rotation:{chosen} (tested_waves={perf['total_waves']})",
        }

    # Preserve 20% exploration slot on least-tested/runner-up strategy so adaptation stays live.
    if idx % 5 == 4:
        others = [s for s in _EXPLORE_ORDER if s != fastest["recommended_strategy"]]
        others.sort(key=lambda s: (rows_by_strat[s]["waves_run"], -(rows_by_strat[s]["sticky_score"] or 0.0)))
        explore = others[0]
        return {
            "recommended_strategy": explore,
            "recommended_label": STRATEGY_SPECS[explore]["label"],
            "selection_mode": "explore_slot",
            "confidence": "measured",
            "reason": f"explore_slot:{explore} (winner={fastest['recommended_strategy']})",
        }

    chosen = fastest["recommended_strategy"]
    return {
        "recommended_strategy": chosen,
        "recommended_label": STRATEGY_SPECS[chosen]["label"],
        "selection_mode": "empirical_winner",
        "confidence": "measured",
        "reason": f"empirical_winner:{chosen} (score={rows_by_strat[chosen]['sticky_score']})",
    }


def _build_follower_trend(
    ledger: dict[str, Any],
    wave_records: list[dict[str, Any]],
    days: list[str],
    daily: dict[str, dict[str, int]],
    *,
    now_epoch: float,
) -> dict[str, Any]:
    """Compute honest multi-day follower growth & sticky retention trend from real observations."""
    snapshots = _collect_follower_snapshots(ledger)
    _, inbound_events = _attribute_growth_and_inbound(ledger, wave_records, snapshots, now_epoch=now_epoch)

    latest_snap = snapshots[-1] if snapshots else None
    current_followers = latest_snap["followers_count"] if latest_snap else None
    current_following = latest_snap["following_count"] if latest_snap else None
    ratio = (
        round(current_followers / current_following, 3)
        if isinstance(current_followers, int) and isinstance(current_following, int) and current_following > 0
        else None
    )

    window_start_ep = now_epoch - 7 * 86400.0
    in_window_snaps = [s for s in snapshots if s["at_epoch"] >= window_start_ep]
    baseline_snap = in_window_snaps[0] if len(in_window_snaps) >= 2 else (snapshots[0] if len(snapshots) >= 2 else None)
    net_change_7d = (
        int(current_followers - baseline_snap["followers_count"])
        if current_followers is not None and baseline_snap is not None and baseline_snap is not latest_snap
        else None
    )
    growth_rate_7d_pct = (
        round((net_change_7d / baseline_snap["followers_count"]) * 100.0, 2)
        if net_change_7d is not None and baseline_snap is not None and baseline_snap["followers_count"] > 0
        else None
    )

    net_change_24h = None
    if current_followers is not None and len(snapshots) >= 2:
        earlier_24h = [
            s for s in snapshots[:-1]
            if 12 * 3600.0 <= (latest_snap["at_epoch"] - s["at_epoch"]) <= 48 * 3600.0
        ]
        if earlier_24h:
            net_change_24h = int(current_followers - earlier_24h[-1]["followers_count"])

    today_str = days[-1] if days else _local_date(now_epoch).isoformat()
    inbound_by_day: dict[str, int] = {d: 0 for d in days}
    sticky_by_day: dict[str, int] = {d: 0 for d in days}
    inbound_total = len(inbound_events)
    sticky_total = sum(1 for ev in inbound_events if ev["sticky"])
    for ev in inbound_events:
        d = ev["date"]
        if d in inbound_by_day:
            inbound_by_day[d] += 1
            if ev["sticky"]:
                sticky_by_day[d] += 1

    snaps_by_day: dict[str, dict[str, Any]] = {}
    for s in snapshots:
        snaps_by_day[s["date"]] = s

    strats_by_day: dict[str, dict[str, int]] = {}
    for w in wave_records:
        d = w["date"]
        strats_by_day.setdefault(d, {})
        strats_by_day[d][w["strategy"]] = strats_by_day[d].get(w["strategy"], 0) + 1

    prev_observed_fc: int | None = None
    for s in snapshots:
        if s["date"] < days[0]:
            prev_observed_fc = s["followers_count"]

    daily_series: list[dict[str, Any]] = []
    for d in days:
        day_snap = snaps_by_day.get(d)
        fc = day_snap["followers_count"] if day_snap else None
        fg = day_snap["following_count"] if day_snap else None
        delta = (fc - prev_observed_fc) if (fc is not None and prev_observed_fc is not None) else None
        if fc is not None:
            prev_observed_fc = fc
        day_strats = strats_by_day.get(d, {})
        dominant_strat = (
            max(day_strats.items(), key=lambda kv: kv[1])[0]
            if day_strats
            else None
        )
        act_counts = daily.get(d, {})
        daily_series.append({
            "date": d,
            "followers_count": fc,
            "following_count": fg,
            "follower_delta": delta,
            "inbound_new": inbound_by_day.get(d, 0),
            "sticky_inbound": sticky_by_day.get(d, 0),
            "verified_actions": sum(int(v) for v in act_counts.values()),
            "dominant_strategy": dominant_strat,
            "dominant_strategy_label": STRATEGY_SPECS[dominant_strat]["label"] if dominant_strat in STRATEGY_SPECS else None,
        })

    data_source = (
        "profile_snapshots"
        if snapshots
        else ("inbound_signals_only" if inbound_total > 0 else "no_observations_yet")
    )
    sticky_rate = round(sticky_total / inbound_total, 3) if inbound_total > 0 else 0.0
    return {
        "current_followers": current_followers,
        "current_following": current_following,
        "follower_ratio": ratio,
        "baseline_followers_7d": baseline_snap["followers_count"] if baseline_snap else None,
        "net_change_24h": net_change_24h,
        "net_change_7d": net_change_7d,
        "growth_rate_7d_pct": growth_rate_7d_pct,
        "inbound_new_today": inbound_by_day.get(today_str, 0),
        "inbound_new_7d": sum(inbound_by_day.values()),
        "known_inbound_followers": inbound_total,
        "sticky_followers": sticky_total,
        "sticky_rate": sticky_rate,
        "snapshots_count": len(snapshots),
        "last_observed_at": latest_snap["at"] if latest_snap else None,
        "data_source": data_source,
        "daily_series": daily_series,
    }


def build_ops_summary(
    ledger: dict[str, Any],
    autopilot_status: dict[str, Any],
    *,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Summarize 7-day activity, follower growth trend, strategy comparison, and recent waves."""
    now = float(now_epoch) if now_epoch is not None else time.time()
    today = _local_date(now)
    days = [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    daily: dict[str, dict[str, int]] = {
        d: {"publish_post": 0, "like_post": 0, "follow_profile": 0, "repost_post": 0, "reply_post": 0}
        for d in days
    }
    totals = {"completed": 0, "uncertain": 0, "failed": 0}
    follows_verified = 0
    follows_mutual = 0
    mutual_map = ledger.get("mutual_followers") if isinstance(ledger.get("mutual_followers"), dict) else {}
    mutual_handles = {str(k).strip().lstrip("@").casefold() for k in mutual_map.keys() if str(k).strip()}
    recent_actions: list[tuple[float, dict[str, Any]]] = []
    latest_snapshot: tuple[float, dict[str, Any]] | None = None
    _WRITE_TYPES = {"publish_post", "like_post", "repost_post", "reply_post", "follow_profile", "unfollow_profile"}

    actions = ledger.get("actions")
    for action in (actions.values() if isinstance(actions, dict) else ()):
        if not isinstance(action, dict):
            continue
        status = action.get("status")
        at_epoch = _parse_epoch(action.get("completed_at")) or _parse_epoch(action.get("created_at"))
        if at_epoch is None:
            continue
        atype = str(action.get("type") or "")
        result = action.get("result") or {}
        payload = action.get("payload") or {}
        if atype in {"observe_account", "owner_read_account"} and status == "completed" and isinstance(result, dict):
            snap = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else result
            if snap.get("username") and (latest_snapshot is None or at_epoch > latest_snapshot[0]):
                latest_snapshot = (at_epoch, snap)
        if atype in _WRITE_TYPES:
            target_url = (
                result.get("reply_url")
                or result.get("post_url")
                or result.get("profile_url")
                or payload.get("post_url")
                or payload.get("profile_url")
                or payload.get("url")
                or ""
            )
            handle = (
                result.get("handle")
                or result.get("author")
                or payload.get("handle")
                or payload.get("author")
                or ""
            )
            recent_actions.append((
                at_epoch,
                {
                    "action_id": action.get("action_id"),
                    "type": atype,
                    "status": status,
                    "verified": bool(result.get("verified") is True),
                    "handle": str(handle),
                    "url": str(target_url),
                    "text": str(result.get("text") or payload.get("text") or "")[:120],
                    "interaction_role": payload.get("interaction_role"),
                    "visited_before_engage": bool(payload.get("visited_before_engage")),
                    "at": action.get("completed_at") or action.get("created_at"),
                },
            ))
        day_str = _local_date(at_epoch).isoformat()
        if day_str not in daily:
            continue
        if status in totals:
            totals[status] += 1
        if status != "completed":
            continue
        if result.get("verified") is not True:
            continue
        if atype in daily[day_str]:
            daily[day_str][atype] += 1
        if atype == "follow_profile":
            follows_verified += 1
            f_handle = str(result.get("handle") or payload.get("handle") or "").strip().lstrip("@").casefold()
            if result.get("mutual_verified") is True or (f_handle and f_handle in mutual_handles):
                follows_mutual += 1

    if latest_snapshot is None and isinstance(ledger.get("account"), dict) and ledger["account"].get("username"):
        latest_snapshot = (0.0, ledger["account"])
    # Merge latest follower count into last_account_snapshot if recorded later via follower_snapshots.
    snaps_all = _collect_follower_snapshots(ledger)
    account_snap_out = dict(latest_snapshot[1]) if latest_snapshot else None
    if snaps_all:
        newest_s = snaps_all[-1]
        if account_snap_out is None and isinstance(ledger.get("account"), dict):
            account_snap_out = dict(ledger["account"])
        if account_snap_out is not None:
            if newest_s.get("followers_count") is not None:
                account_snap_out["followers_count"] = newest_s["followers_count"]
            if newest_s.get("following_count") is not None:
                account_snap_out["following_count"] = newest_s["following_count"]
            if newest_s.get("posts_count") is not None:
                account_snap_out["posts_count"] = newest_s["posts_count"]

    wave_records = _extract_wave_records(ledger, autopilot_status)
    follower_trend = _build_follower_trend(ledger, wave_records, days, daily, now_epoch=now)
    strategy_eval = evaluate_strategy_performance(ledger, autopilot_status=autopilot_status, now_epoch=now)
    series_by_date = {row["date"]: row for row in follower_trend["daily_series"]}

    total_attempts = totals["completed"] + totals["uncertain"] + totals["failed"]
    uncertain_rate = round(totals["uncertain"] / total_attempts, 3) if total_attempts else 0.0
    follow_back_rate = round(follows_mutual / follows_verified, 3) if follows_verified else 0.0
    recent_actions.sort(key=lambda pair: pair[0], reverse=True)
    today_row = daily.get(today.isoformat(), {})
    replies_today = int(today_row.get("reply_post", 0))
    likes_today = int(today_row.get("like_post", 0))

    tz_label = datetime.now().astimezone().tzname() or "local"
    return {
        "window_days": 7,
        "timezone": tz_label,
        "account_binding": ledger.get("account_binding"),
        "last_account_snapshot": account_snap_out,
        "totals": totals,
        "uncertain_rate": uncertain_rate,
        "follow_funnel": {
            "verified_follows": follows_verified,
            "mutual_verified": follows_mutual,
            "known_inbound_followers": len(mutual_handles),
            "follow_back_rate": follow_back_rate,
        },
        "interaction_quality": {
            "replies_today": replies_today,
            "likes_today": likes_today,
            "bundled_actions_recent": sum(1 for _, a in recent_actions[:30] if a.get("visited_before_engage") or a.get("interaction_role") in {"outbound_bundle", "inbound_reply", "inbound_followback"}),
        },
        "follower_trend": follower_trend,
        "strategy_performance": strategy_eval["leaderboard"],
        "fastest_strategy": strategy_eval["fastest_strategy"],
        "strategies_catalog": list_orchestration_strategies(),
        "daily_breakdown": [
            {
                "date": d,
                **daily[d],
                **(
                    {
                        "followers_count": series_by_date[d]["followers_count"],
                        "follower_delta": series_by_date[d]["follower_delta"],
                        "inbound_new": series_by_date[d]["inbound_new"],
                        "dominant_strategy": series_by_date[d]["dominant_strategy"],
                    }
                    if d in series_by_date
                    else {}
                ),
            }
            for d in days
        ],
        "recent_actions": [item for _, item in recent_actions[:30]],
        "autopilot": autopilot_status,
    }


_ANALYTICS_LOCKS_GUARD = threading.Lock()
_ANALYTICS_LOCKS: dict[str, LedgerLock] = {}


def _analytics_lock_for(state_dir: Path) -> LedgerLock:
    key = os.path.abspath(str(state_dir))
    with _ANALYTICS_LOCKS_GUARD:
        lock = _ANALYTICS_LOCKS.get(key)
        if lock is None:
            lock = LedgerLock(state_dir / ".analytics.lock")
            _ANALYTICS_LOCKS[key] = lock
        return lock


def _atomic_write_text(path: Path, content: str, *, encoding: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp.open("w", encoding=encoding, newline="") as handle:
            handle.write(content)
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05)
    finally:
        tmp.unlink(missing_ok=True)


def persist_daily_metrics(state_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    """Persist cumulative daily metrics into analytics_history.json and daily_metrics.csv for human maintenance."""
    state_dir.mkdir(parents=True, exist_ok=True)
    history_path = state_dir / "analytics_history.json"
    csv_path = state_dir / "daily_metrics.csv"
    with _analytics_lock_for(state_dir):
        return _persist_daily_metrics_locked(history_path, csv_path, summary)


def _persist_daily_metrics_locked(history_path: Path, csv_path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    import csv
    import io

    tz_target = str(summary.get("timezone", "Asia/Shanghai"))
    history: dict[str, Any] = {"version": 1, "timezone": tz_target, "days": {}}
    loaded_ok = False
    if history_path.exists():
        try:
            loaded = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("days"), dict):
                history = loaded
                loaded_ok = True
        except ValueError:
            pass

    funnel = summary.get("follow_funnel") or {}
    uncertain_rate = summary.get("uncertain_rate", 0.0)
    days_map: dict[str, dict[str, Any]] = history.setdefault("days", {})
    daily_rows = summary.get("daily_breakdown") or []
    today_date = daily_rows[-1].get("date") if daily_rows else None
    changed = (not loaded_ok) or (not csv_path.exists()) or (history.get("timezone") != tz_target)
    for row in daily_rows:
        d = str(row.get("date") or "").strip()
        if not d:
            continue
        prev = days_map.get(d) if isinstance(days_map.get(d), dict) else {}
        fc_val = row.get("followers_count")
        fd_val = row.get("follower_delta")
        in_val = row.get("inbound_new")
        dom_strat = row.get("dominant_strategy")
        new_day = {
            "date": d,
            "publish_post": int(row.get("publish_post", 0)),
            "like_post": int(row.get("like_post", 0)),
            "follow_profile": int(row.get("follow_profile", 0)),
            "repost_post": int(row.get("repost_post", 0)),
            "reply_post": int(row.get("reply_post", 0)),
            "verified_follows": int(funnel.get("verified_follows", prev.get("verified_follows", 0))) if d == today_date else int(prev.get("verified_follows", 0)),
            "mutual_verified": int(funnel.get("mutual_verified", prev.get("mutual_verified", 0))) if d == today_date else int(prev.get("mutual_verified", 0)),
            "follow_back_rate": float(funnel.get("follow_back_rate", prev.get("follow_back_rate", 0.0))) if d == today_date else float(prev.get("follow_back_rate", 0.0)),
            "uncertain_rate": float(uncertain_rate) if d == today_date else float(prev.get("uncertain_rate", 0.0)),
            "followers_count": int(fc_val) if isinstance(fc_val, int) and not isinstance(fc_val, bool) else prev.get("followers_count"),
            "follower_delta": int(fd_val) if isinstance(fd_val, int) and not isinstance(fd_val, bool) else prev.get("follower_delta"),
            "inbound_new": int(in_val) if isinstance(in_val, int) and not isinstance(in_val, bool) else int(prev.get("inbound_new", 0)),
            "dominant_strategy": str(dom_strat) if dom_strat else prev.get("dominant_strategy"),
        }
        if prev != new_day:
            changed = True
        days_map[d] = new_day

    if not changed:
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                csv_text = handle.read()
            if csv_text:
                return {
                    "history_json": str(history_path),
                    "metrics_csv": str(csv_path),
                    "days_recorded": len(days_map),
                    "csv_content": csv_text,
                }
        except OSError:
            pass

    history["timezone"] = tz_target
    history["updated_at"] = _iso(time.time())
    _atomic_write_text(history_path, json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    cols = [
        "date",
        "publish_post",
        "like_post",
        "follow_profile",
        "repost_post",
        "reply_post",
        "verified_follows",
        "mutual_verified",
        "follow_back_rate",
        "uncertain_rate",
        "followers_count",
        "follower_delta",
        "inbound_new",
        "dominant_strategy",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    for d in sorted(days_map.keys()):
        writer.writerow({k: ("" if days_map[d].get(k) is None else days_map[d].get(k, "")) for k in cols})
    csv_text = buf.getvalue()
    _atomic_write_text(csv_path, csv_text, encoding="utf-8-sig")

    return {
        "history_json": str(history_path),
        "metrics_csv": str(csv_path),
        "days_recorded": len(days_map),
        "csv_content": csv_text,
    }


class AutopilotScheduler:
    """Lightweight background daemon thread that runs due autopilot waves and session keep-alive."""

    def __init__(
        self,
        tick_fn: Any,
        *,
        poll_seconds: float = 60.0,
        keepalive_fn: Any | None = None,
        keepalive_interval_seconds: float = 1500.0,
    ) -> None:
        import threading

        self._tick_fn = tick_fn
        self._keepalive_fn = keepalive_fn
        self._keepalive_interval_seconds = max(300.0, float(keepalive_interval_seconds))
        self._poll_seconds = max(1.0, float(poll_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_error: str | None = None
        self._last_tick_at: str | None = None
        self._last_outcome: str | None = None
        self._last_keepalive_at: str | None = None
        self._last_keepalive_epoch: float = 0.0
        self._tick_log: list[dict[str, str]] = []
        self._ticks = 0

    def start(self) -> bool:
        import threading

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="x-autopilot-scheduler", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> bool:
        with self._lock:
            t = self._thread
            self._stop_event.set()
            self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
            return True
        return False

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return {
                "running": running,
                "poll_seconds": self._poll_seconds,
                "ticks": self._ticks,
                "last_tick_at": self._last_tick_at,
                "last_outcome": self._last_outcome,
                "last_keepalive_at": self._last_keepalive_at,
                "tick_log": list(self._tick_log[-20:]),
                "last_error": self._last_error,
            }

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            now_str = _iso(time.time())
            outcome_str = "checked"
            try:
                res = self._tick_fn()
                if isinstance(res, dict):
                    if res.get("ran") is True:
                        exe = res.get("execution") or {}
                        outcome_str = f"ran wave ({exe.get('verified_changed', 0)}/{exe.get('processed', 0)} verified)"
                        self._last_keepalive_epoch = time.time()
                        self._last_keepalive_at = now_str
                    elif res.get("reason"):
                        outcome_str = f"waiting ({res['reason']})"
                        if (
                            self._keepalive_fn is not None
                            and res.get("reason") == "WAVE_INTERVAL_NOT_ELAPSED"
                            and (time.time() - self._last_keepalive_epoch) >= self._keepalive_interval_seconds
                        ):
                            try:
                                ka = self._keepalive_fn()
                                self._last_keepalive_epoch = time.time()
                                self._last_keepalive_at = _iso(self._last_keepalive_epoch)
                                outcome_str = f"keepalive ({ka.get('status', 'ok')})" if isinstance(ka, dict) else "keepalive (ok)"
                            except Exception as ka_exc:  # noqa: BLE001
                                outcome_str = f"keepalive_err ({str(ka_exc)[:80]})"
                with self._lock:
                    self._last_error = None
                    self._last_tick_at = now_str
                    self._last_outcome = outcome_str
                    self._tick_log.append({"at": now_str, "outcome": outcome_str})
                    self._tick_log = self._tick_log[-20:]
            except Exception as exc:  # noqa: BLE001
                err_str = str(exc)[:200]
                with self._lock:
                    self._last_error = err_str
                    self._last_tick_at = now_str
                    self._last_outcome = f"error: {err_str}"
                    self._tick_log.append({"at": now_str, "outcome": f"error: {err_str}"})
                    self._tick_log = self._tick_log[-20:]
            with self._lock:
                self._ticks += 1
            self._stop_event.wait(self._poll_seconds)
