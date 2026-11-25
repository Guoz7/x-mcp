"""Personal X account health, safe pacing, content queue, and daily orchestration.

This module provides the policy and state-derivation layer for maintaining a
personal X account safely:

1. Circuit breaker & safe pacing enforcement (from `docs/orchestration-profile-safe.json`
   and `account_state.py`), stopping immediately on `141`, `37`, `283`, `161`,
   429 rate-limits, or consecutive `uncertain` outcomes.
2. Account standing & warmup health score (`0-100`) combining profile readiness,
   original-content cadence, balanced engagement, and safety cleanliness.
3. Durable personal content queue (`content_queue.json`) for scheduled original
   posts and replies.
4. Unified daily activity planner (`build_daily_orchestration_plan`) that
   combines due original posts, deduplicated timeline likes/reposts, and
   deduplicated follow candidates into a single human-paced, bounded wave.
"""

from __future__ import annotations

import json
import os
import random
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from account_state import classify_refusal
from daily_action_budget import (
    LOCAL_TIMEZONE,
    _local_date,
    _parse_epoch,
    evaluate_daily_action_budget,
)
from ledger_lock import LedgerLock

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAFE_PROFILE_PATH = ROOT / "docs" / "orchestration-profile-safe.json"

DEFAULT_SAFE_CAPS: dict[str, dict[str, Any]] = {
    "follow_profile": {"enabled": True, "per_day": 25, "min_gap_seconds": 25, "max_gap_seconds": 65},
    "repost_post": {"enabled": True, "per_day": 8, "min_gap_seconds": 150, "max_gap_seconds": 360},
    "like_post": {"enabled": True, "per_day": 60, "min_gap_seconds": 20, "max_gap_seconds": 55},
    "reply_post": {"enabled": True, "per_day": 25, "min_gap_seconds": 75, "max_gap_seconds": 145},
    "publish_post": {"enabled": True, "per_day": 5, "min_gap_seconds": 600, "max_gap_seconds": 1800},
}

DEFAULT_CIRCUIT_BREAKER: dict[str, Any] = {
    "stop_on_any_refusal": True,
    "refusal_codes": ["283", "161", "141", "37"],
    "stop_on_uncertain_streak": 2,
    "cooldown_hours_after_refusal": 24,
    "cooldown_hours_after_uncertain_streak": 0.5,
    "do_not_switch_action_kind": True,
}

DEFAULT_WARMUP: dict[str, Any] = {
    "required_days_before_automation": 7,
    "required_original_posts": 10,
    "required_profile_fields": ["name", "bio", "avatar"],
}

_SECRET_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]+PRIVATE KEY-----)"
)
_EXTERNAL_URL_RE = re.compile(r"https?://(?!(?:www\.)?(?:x\.com|twitter\.com|t\.co)\b)\S+", re.IGNORECASE)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_AI_CLICHE_PATTERNS = (
    "great post",
    "very insightful",
    "thanks for sharing",
    "totally agree",
    "well said",
    "this is huge",
    "game changer",
    "solid breakdown",
    "solid note on",
    "delicate balance",
    "respect the pragmatic",
    "beat flashy demos",
    "hard-won insight",
    "glad to follow your build",
    "teach twice as much",
    "curious what guardrail",
    "deep dive",
    "food for thought",
    "love this take",
    "couldn't agree more",
    "this resonates",
    "keep pushing",
    "in the long run",
    "感谢分享",
    "感谢互动交流",
    "很有深度",
    "说得太好了",
    "非常赞同",
    "受益匪浅",
    "收到你的补充",
    "实战踩坑",
    "底层工程闭环",
    "后验确认",
    "同路人常联系",
    "留个脚印关注",
    "稳步往上走",
    "最有收获的是",
    "值得一试",
    "期待你的更多",
    "期待更多分享",
    "给你点赞支持",
    "从工程角度",
    "从长远来看",
    "总的来说",
    "总而言之",
    "干货满满",
    "共勉",
)


def check_reply_naturalness(
    text: str,
    *,
    extra_banned: list[str] | None = None,
) -> list[str]:
    """Return warnings if a reply reads like generic AI boilerplate."""
    cleaned = str(text or "").strip()
    lower = cleaned.casefold()
    warnings: list[str] = []
    for phrase in (*_AI_CLICHE_PATTERNS, *(extra_banned or [])):
        p = str(phrase or "").strip().casefold()
        if p and p in lower:
            warnings.append(f"Avoid generic or banned phrase: '{phrase}'")
    if "——" in cleaned:
        warnings.append("Avoid PPT-style em-dash summaries.")
    if cleaned.count("#") >= 2:
        warnings.append("Avoid multiple hashtags in replies.")
    if len(cleaned) < 8:
        warnings.append("Reply is too short to add a concrete point.")
    return warnings


def _recent_reply_texts(ledger: dict[str, Any] | None, *, max_items: int = 400) -> list[str]:
    if not isinstance(ledger, dict):
        return []
    out: list[tuple[float, str]] = []
    for action in (ledger.get("actions") or {}).values():
        if not isinstance(action, dict) or action.get("type") != "reply_post":
            continue
        if action.get("status") not in {"completed", "pending", "claimed"}:
            continue
        payload = action.get("payload") or {}
        result = action.get("result") or {}
        txt = str(result.get("text") or payload.get("text") or "").strip()
        if not txt:
            continue
        ep = _parse_epoch(action.get("completed_at") or action.get("created_at")) or 0.0
        out.append((ep, txt))
    out.sort(key=lambda pair: pair[0], reverse=True)
    return [txt for _, txt in out[:max_items]]


_TECH_ANCHORS = (
    ("mcp", "MCP"),
    ("playwright", "Playwright"),
    ("typescript", "TypeScript"),
    ("javascript", "JS"),
    ("python", "Python"),
    ("rust", "Rust"),
    ("golang", "Go"),
    ("sqlite", "SQLite"),
    ("postgres", "Postgres"),
    ("docker", "Docker"),
    ("kubernetes", "K8s"),
    ("claude", "Claude"),
    ("codex", "Codex"),
    ("gemini", "Gemini"),
    ("openai", "OpenAI"),
    ("deepseek", "DeepSeek"),
    ("oauth", "OAuth"),
    ("browser", "浏览器执行层"),
    ("rag", "RAG"),
    ("eval", "评测/Eval"),
    ("latency", "延迟"),
    ("rate limit", "限流"),
    ("retry", "重试兜底"),
    ("lock", "并发锁"),
    ("cache", "缓存"),
    ("workflow", "工作流编排"),
    ("agent", "Agent 链路"),
    ("llm", "大模型"),
    ("saas", "SaaS"),
    ("open source", "开源项目"),
    ("build in public", "Build in Public"),
    ("indie", "独立开发"),
    ("独立开发", "独立开发"),
    ("开源", "开源实现"),
    ("并发", "并发与状态同步"),
    ("限流", "限流与风控"),
    ("自动化", "自动化工作流"),
    ("架构", "系统架构"),
    ("评测", "评测基准"),
    ("部署", "线上部署"),
    ("工具链", "工具链"),
)


_WS_COLLAPSE_RE = re.compile(r"\s+")
_REPLY_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]{2,}")


class _ReplyDeduper:
    """Fast incremental exact, prefix, and token-Jaccard duplicate detector across 400+ daily replies."""

    def __init__(self, recent_replies: list[str] | set[str] | None = None, *, prefix_window: int = 80) -> None:
        self.prefix_window = max(15, int(prefix_window))
        self.exact_norms: set[str] = set()
        self.recent_prefixes: list[str] = []
        self.token_sets: list[frozenset[str]] = []
        self._token_index: dict[str, list[int]] = {}
        for item in (recent_replies or []):
            self.add(str(item or ""))

    def _norm(self, text: str) -> str:
        return _WS_COLLAPSE_RE.sub(" ", text.strip().casefold())

    def add(self, text: str) -> None:
        norm = self._norm(text)
        if not norm or norm in self.exact_norms:
            return
        self.exact_norms.add(norm)
        self.recent_prefixes.append(norm[:14])
        tokens = frozenset(_REPLY_TOKEN_RE.findall(norm))
        if len(tokens) >= 4:
            idx = len(self.token_sets)
            self.token_sets.append(tokens)
            for tok in tokens:
                self._token_index.setdefault(tok, []).append(idx)

    def is_duplicate(self, candidate: str) -> bool:
        norm = self._norm(candidate)
        if not norm:
            return True
        if norm in self.exact_norms:
            return True
        cand_prefix = norm[:14]
        if len(cand_prefix) >= 10:
            for pnorm in self.recent_prefixes[-self.prefix_window:]:
                if pnorm.startswith(cand_prefix):
                    return True
        cand_tokens = frozenset(_REPLY_TOKEN_RE.findall(norm))
        if len(cand_tokens) >= 4:
            overlap_counts: dict[int, int] = {}
            for tok in cand_tokens:
                for idx in self._token_index.get(tok, ()):
                    overlap_counts[idx] = overlap_counts.get(idx, 0) + 1
            cand_len = len(cand_tokens)
            for idx, shared in overlap_counts.items():
                ptokens = self.token_sets[idx]
                if (shared / max(cand_len, len(ptokens))) >= 0.72:
                    return True
        return False


def _is_duplicate_reply(candidate: str, recent_replies: list[str] | set[str] | _ReplyDeduper | None) -> bool:
    if not recent_replies:
        return False
    if isinstance(recent_replies, _ReplyDeduper):
        return recent_replies.is_duplicate(candidate)
    norm = _WS_COLLAPSE_RE.sub(" ", candidate.strip().casefold())
    if not norm:
        return True
    cand_prefix = norm[:14]
    recent_list = [str(x or "").strip() for x in recent_replies]
    if len(cand_prefix) >= 10:
        for prev in recent_list[-80:]:
            pnorm = _WS_COLLAPSE_RE.sub(" ", prev.casefold())
            if pnorm and pnorm.startswith(cand_prefix):
                return True
    cand_tokens = frozenset(_REPLY_TOKEN_RE.findall(norm))
    for prev in recent_list:
        pnorm = _WS_COLLAPSE_RE.sub(" ", prev.casefold())
        if not pnorm:
            continue
        if norm == pnorm:
            return True
        if len(cand_tokens) >= 4:
            ptokens = frozenset(_REPLY_TOKEN_RE.findall(pnorm))
            if ptokens and (len(cand_tokens & ptokens) / max(len(cand_tokens), len(ptokens))) >= 0.72:
                return True
    return False


_ELITE_TECH_SIGNALS = (
    "benchmark", "latency", "p99", "p95", "throughput", "qps", "rps", "architecture",
    "postmortem", "concurrency", "idempoten", "state machine", "mutex", "wal",
    "flamegraph", "profil", "race condition", "deadlock", "backpressure", "cdp",
    "compiler", "kernel", "ebpf", "tokio", "github.com/", "arxiv.org", "ablation",
    "基准", "压测", "延迟", "吞吐", "架构", "复盘", "剖析", "并发", "状态机",
    "幂等", "死锁", "源码", "底层", "评测",
)
_SOCIAL_CHATTER_RE = re.compile(
    r"(?:出摊|来聊天|交朋友|蓝\s*v|活人一枚|早安|晚安|\bgm\b|\bgn\b|互关|互粉|互赞|giveaway|airdrop)",
    re.IGNORECASE,
)
_TECH_METRIC_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|us|ns|s|x|%|qps|rps|mb|kb|gb|tokens?|fps)\b",
    re.IGNORECASE,
)


def score_technical_quality(
    post: dict[str, Any],
    *,
    persona_topics: list[str] | None = None,
) -> tuple[int, bool, list[str]]:
    """Score whether a post is substantive engineering/tech content suitable for rare reposts."""
    text = str(post.get("text") or "").strip()
    bio = str(post.get("author_bio") or post.get("bio") or "").strip()
    if not text:
        return (0, False, ["empty"])
    lower = text.casefold()
    bio_lower = bio.casefold()
    non_url = re.sub(r"https?://\S+", "", text).strip()
    reasons: list[str] = []

    if len(non_url) < 35:
        return (0, False, ["too_short_after_links"])
    if text.count("#") >= 4:
        return (0, False, ["hashtag_spam"])
    if _SOCIAL_CHATTER_RE.search(text) or has_reciprocal_marker({"text": text, "bio": bio}):
        return (0, False, ["social_or_reciprocal_bait"])
    if any(spam in f"{lower} {bio_lower}" for spam in ("crypto pump", "100x", "airdrop", "whitelist", "presale")):
        return (0, False, ["promo_bait"])

    score = 0
    matched_anchors: list[str] = []
    for needle, label in _TECH_ANCHORS:
        if needle in lower and label not in matched_anchors:
            matched_anchors.append(label)
    score += min(4, len(matched_anchors)) * 12
    if matched_anchors:
        reasons.append("anchors:" + ",".join(matched_anchors[:4]))

    deep_hits = sum(1 for sig in _ELITE_TECH_SIGNALS if sig in lower)
    if deep_hits:
        score += min(3, deep_hits) * 14
        reasons.append(f"engineering_depth:{deep_hits}")

    if _TECH_METRIC_RE.search(text):
        score += 14
        reasons.append("quantitative_metrics")

    if len(non_url) >= 75:
        score += 10
        reasons.append("substantive_length")
    elif len(non_url) < 50 and len(matched_anchors) < 2 and deep_hits == 0:
        return (score, False, ["insufficient_depth"])

    topics = [str(t).strip().casefold() for t in (persona_topics or []) if str(t).strip()]
    topic_hits = sum(1 for t in topics if t in lower)
    if topic_hits:
        score += min(2, topic_hits) * 6

    if any(needle in bio_lower for needle, _ in _TECH_ANCHORS[:18]):
        score += 6

    is_elite = score >= 38 and (len(matched_anchors) >= 1 or deep_hits >= 1)
    return (score, is_elite, reasons)


_HANDLE_LINE_RE = re.compile(r"^@[A-Za-z0-9_]{1,15}$")
_TIME_LINE_RE = re.compile(
    r"^(?:\d+[smhdw]|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}(?:,\s*\d{4})?)$",
    re.IGNORECASE,
)
_METRIC_LINE_RE = re.compile(r"^(?:\d+(?:\.\d+)?[kmb]?|\d{1,3}(?:,\d{3})+)$", re.IGNORECASE)
_META_NOISE_RE = re.compile(
    r"^(?:·|replying to|and|translated from\b.*|show original|show more|quote)$",
    re.IGNORECASE,
)

_MILESTONE_RE = re.compile(
    r"(?:フォロワー|followers|粉丝|互关).{0,18}(?:\d+|超え|達成|破百|破千|milestone|reached|hit)|"
    r"(?:\d+|破百|破千).{0,10}(?:人|followers|粉丝).{0,12}(?:超え|達成|おめでとう|congrats|突破|了)",
    re.IGNORECASE,
)
_CREATIVE_RE = re.compile(
    r"(?:小説|narou|syosetu|イラスト|漫画|絵を描|小说|写小说|连载|画画|配乐|novel|fiction)",
    re.IGNORECASE,
)
_LANG_LEARN_RE = re.compile(
    r"(?:学语言|学英语|学日语|学韩语|背单词|练口语|外语|日本語勉強|英語勉強|language learning|practicing (?:english|japanese|chinese))",
    re.IGNORECASE,
)
_VENT_RANT_RE = re.compile(
    r"(?:加班|破防|好累|累死|疲|睡不着|失眠|凌晨|半夜|头秃|崩溃|无语|难顶|卡死|报错|封号|限流|写吐了|麻了|烦死|"
    r"burnout|exhausted|overtime|debug hell|rate limit|can't sleep|staring at|ruin my|flak(?:y|ing)|brain is fried|1am|2am|3am|midnight|tired|\bugh\b)",
    re.IGNORECASE,
)
_SOCIAL_CHAT_RE = re.compile(
    r"(?:出摊|活人|交朋友|聊天|唠嗑|摸鱼|周末|冰美式|咖啡|早安|晚安|午安|逛逛|相互フォロー|フォロバ|よろしく|仲良く|"
    r"おはよう|おやすみ|週末|まったり|good morning|weekend|coffee|\bgm\b|\bgn\b)",
    re.IGNORECASE,
)
_STRICT_BODY_TECH_TOKENS = (
    "mcp", "rust", "python", "golang", "typescript", "javascript", "playwright", "docker",
    "kubernetes", "sqlite", "postgres", "tokio", "llvm", "git", "nginx", "servo", "ebpf",
    "benchmark", "latency", "p99", "p95", "mutex", "wal", "lock", "retry", "backoff",
    "scheduler", "agent", "llm", "claude", "codex", "gemini", "deepseek", "openai",
    "eval", "selector", "dom", "oauth", "workflow", "build in public", "indie", "ship",
    "saas", "architecture", "compiler", "concurrency", "idempoten", "state machine",
    "guardrail", "circuit breaker", "abort", "debug", "github", "repo", "api",
    "开源", "代码", "并发", "状态机", "架构", "部署", "评测", "压测", "延迟", "脚本",
    "浏览器", "编译器", "写程序", "自动化", "限流", "调试", "接口", "工具链", "系统架构",
    "エージェント", "実装", "検証", "設計", "自動化", "デバッグ", "テスト",
    "에이전트", "자동화", "테스트", "워크플로", "아키텍처", "구현",
)


def clean_post_body(raw_text: str) -> str:
    """Strip X card display-name, @handle, timestamp, Replying-to headers, and trailing metrics."""
    lines = [ln.strip() for ln in str(raw_text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""

    # Strip leading card chrome only when recognizable X card markers (@handle or Replying to) appear near top.
    handle_idx = -1
    for i in range(min(4, len(lines))):
        if _HANDLE_LINE_RE.match(lines[i]):
            handle_idx = i
            break
    if handle_idx >= 0:
        idx = handle_idx + 1
        while idx < len(lines) and (
            _META_NOISE_RE.match(lines[idx])
            or _TIME_LINE_RE.match(lines[idx])
            or _HANDLE_LINE_RE.match(lines[idx])
        ):
            idx += 1
        lines = lines[idx:]
    else:
        idx = 0
        while idx < len(lines) and _META_NOISE_RE.match(lines[idx]):
            idx += 1
        lines = lines[idx:]

    while lines and (_METRIC_LINE_RE.match(lines[-1]) or _META_NOISE_RE.match(lines[-1])):
        lines.pop()

    # Handle inline URL split artifacts like "https://\nncode.syosetu.com/..."
    joined = "\n".join(lines).strip()
    joined = re.sub(r"https?://\n+", "https://", joined)
    return joined.strip()


def _classify_post_intent(
    clean_body: str,
    *,
    is_notif: bool,
    notif_kind: str,
) -> str:
    """Classify the actual post intent strictly from cleaned post text."""
    body_no_meta = re.sub(r"https?://\S+|#\S+|@[A-Za-z0-9_]+", "", clean_body).strip()
    lower_body = clean_body.casefold()

    if is_notif and notif_kind == "follow" and (len(body_no_meta) < 6 or "followed you" in lower_body):
        return "notif_follow"
    if len(body_no_meta) < 6:
        return "too_short"

    body_no_tags = re.sub(r"#\S+", " ", clean_body).casefold()
    tech_hits = sum(1 for tok in _STRICT_BODY_TECH_TOKENS if tok in body_no_tags)

    if _CREATIVE_RE.search(clean_body) and tech_hits == 0:
        return "creative"
    if _LANG_LEARN_RE.search(clean_body) and tech_hits == 0:
        return "language_learning"
    if _MILESTONE_RE.search(clean_body) and tech_hits == 0:
        return "milestone"
    if tech_hits >= 1:
        return "tech_discussion"
    if _VENT_RANT_RE.search(clean_body):
        return "vent_rant"
    if _SOCIAL_CHAT_RE.search(clean_body):
        return "social_chatter"
    if is_notif and notif_kind == "follow":
        return "notif_follow"
    return "unknown"


def _detect_engineering_theme(lower_text: str) -> str:
    if any(k in lower_text for k in ("lock", "mutex", "wal", "sqlite", "state machine", "idempoten", "concurrency", "并发", "锁", "状态机", "幂等")):
        return "concurrency_state"
    if any(k in lower_text for k in ("playwright", "dom", "browser", "puppeteer", "chromium", "ocr", "selector", "浏览器", "自動化")):
        return "browser_dom"
    if any(k in lower_text for k in ("429", "rate limit", "backoff", "scheduler", "circuit breaker", "quota", "限流", "风控", "调度", "配额")):
        return "rate_pacing"
    if any(k in lower_text for k in ("latency", "p99", "p95", "throughput", "benchmark", "perf", "qps", "延迟", "压测", "评测", "基准")):
        return "perf_benchmark"
    if any(k in lower_text for k in ("mcp", "agent", "codex", "claude", "llm", "gemini", "openai", "deepseek", "智能体", "大模型")):
        return "agent_tooling"
    return "general_systems"


_LIFE_HOOK_RULES: tuple[tuple[str, str], ...] = (
    ("late_night", r"(?:熬夜|凌晨|半夜|深夜|两点|三点|通宵|失眠|睡不着|晚睡|midnight|late night|insomnia|can't sleep|1am|2am|3am)"),
    ("overtime_tired", r"(?:加班|好累|累死|疲|破防|麻了|无语|烦死|干烧|难顶|burnout|exhausted|overtime|rough day|tired)"),
    ("coffee_break", r"(?:咖啡|冰美式|美式|拿铁|奶茶|喝茶|喝水|摸鱼|coffee|latte|espresso|tea break)"),
    ("weekend_chill", r"(?:周末|散步|出摊|晒太阳|放空|休息|weekend|chill|relax|breather)"),
    ("pet_weather", r"(?:猫|狗|主子|下雨|阴天|降温|风好大|cat|dog|rain|weather)"),
    ("shipping_win", r"(?:上线|跑通|终于过|发布|破百|破千|achieve|milestone|shipped|launched|followers)"),
)


def _extract_life_hooks(clean_body: str) -> list[str]:
    hits: list[str] = []
    for key, pattern in _LIFE_HOOK_RULES:
        if re.search(pattern, clean_body, re.IGNORECASE):
            hits.append(key)
    return hits


def _extract_post_context(post: dict[str, Any], persona_memory: dict[str, Any] | None = None) -> dict[str, Any]:
    """Extract cleaned post text, human intent, language, empathy hooks, and body anchors."""
    raw_text = str(post.get("text") or post.get("text_preview") or "").strip()
    clean_body = clean_post_body(raw_text)
    author = str(post.get("author") or "").strip().lstrip("@")
    notif_kind = str(post.get("notif_kind") or "").strip().lower()
    is_notif = bool(notif_kind) or str(post.get("source") or "") == "notification"

    lang_src = clean_body if clean_body else raw_text
    if re.search(r"[\u3040-\u30ff]", lang_src):
        lang = "ja"
    elif re.search(r"[\uac00-\ud7af]", lang_src):
        lang = "ko"
    elif re.search(r"[\u4e00-\u9fff]", lang_src):
        lang = "zh"
    else:
        lang = "en"

    intent = _classify_post_intent(clean_body, is_notif=is_notif, notif_kind=notif_kind)
    body_lower = re.sub(r"#\S+", " ", clean_body).casefold()
    life_hooks = _extract_life_hooks(clean_body)

    zh_hooks: list[str] = []
    en_hooks: list[str] = []
    for needle, label in _TECH_ANCHORS:
        if needle in body_lower:
            if not re.search(r"[\u4e00-\u9fff]", label):
                if label not in en_hooks:
                    en_hooks.append(label)
            if label not in zh_hooks:
                zh_hooks.append(label)

    theme = _detect_engineering_theme(body_lower) if intent == "tech_discussion" else "casual"
    return {
        "lang": lang,
        "author": author,
        "clean_body": clean_body,
        "intent": intent,
        "theme": theme,
        "life_hooks": life_hooks,
        "is_notif": is_notif,
        "notif_kind": notif_kind,
        "zh_hook": zh_hooks[0] if zh_hooks else "",
        "zh_hook_2": zh_hooks[1] if len(zh_hooks) > 1 else (zh_hooks[0] if zh_hooks else ""),
        "en_hook": en_hooks[0] if en_hooks else "",
        "en_hook_2": en_hooks[1] if len(en_hooks) > 1 else (en_hooks[0] if en_hooks else ""),
        "visited_profile": bool(post.get("visited_profile")),
        "is_build_topic": intent == "tech_discussion" and any(k in body_lower for k in ("独立开发", "开源", "build in public", "indie", "launch", "ship", "saas")),
    }


def analyze_target_post(
    post: dict[str, Any],
    *,
    persona_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Briefly analyze a target post's mood/context, caring angle, and drop-by tactic."""
    ctx = _extract_post_context(post, persona_memory=persona_memory)
    intent = ctx["intent"]
    life_hooks = ctx["life_hooks"]

    if intent in {"too_short", "unknown"}:
        return {
            "clean_body": ctx["clean_body"],
            "lang": ctx["lang"],
            "intent": intent,
            "theme": ctx["theme"],
            "life_hooks": life_hooks,
            "tech_hooks": [h for h in (ctx["zh_hook"], ctx["en_hook"]) if h],
            "author_state": "文字过短或上下文不明，贸然搭话容易像机器人硬聊",
            "care_angle": "点个赞留个友好信号即可，不硬编回复",
            "drop_by_tactic": "like_only_skip_reply",
            "brief_summary": "[跳过硬回] 上下文太短或语义不明，只点1赞最自然",
        }

    state_map = {
        "notif_follow": "刚互关或互动，正活跃看着消息列表",
        "milestone": "刚达成涨粉/上线小节点，心情偏轻松开心",
        "creative": "正在坚持写作/画画等个人创作",
        "language_learning": "在练习外语并欢迎真人顺手接话交流",
        "vent_rant": "熬夜/加班/被报错卡住，情绪偏疲惫或小破防",
        "social_chatter": "平时闲聊/出摊/咖啡休息中，乐意跟真人搭两句",
        "tech_discussion": "在分享开发进度或被真实边界坑折腾",
    }
    care_map = {
        "notif_follow": "先温和打个招呼混个脸熟，别推销自己，像朋友一样说声常逛",
        "milestone": "真诚替对方高兴、夸坚持难得，顺便约好以后在时间线常常见面",
        "creative": "欣赏对方一直有产出这件事本身，叮嘱别太累，完全不扯代码",
        "language_learning": "站在同样靠刷时间线练语感的共鸣点聊一句，轻松自然",
        "vent_rant": "先站同一边安慰情绪、叮嘱喝口水或早点睡别死磕，禁止居高临下给建议",
        "social_chatter": "顺着对方聊的咖啡/周末/出摊/宠物自然接两句家常，让人觉得面前是个活人",
        "tech_discussion": "先体谅排查耗心神的辛苦或肯定跑通不易，再用同行口语聊一句具体痛点",
    }

    author_state = state_map.get(intent, "活跃发布日常")
    care_angle = care_map.get(intent, "先表达共情关心，再顺嘴接话")
    if "late_night" in life_hooks:
        author_state = "大晚上/凌晨还在硬扛未睡"
        care_angle = "重点提醒早点歇着、睡醒思路反而清楚，不强行灌鸡汤"
    elif "coffee_break" in life_hooks and intent == "social_chatter":
        author_state = "正喝咖啡/摸鱼喘口气中"
        care_angle = "顺着咖啡/摸鱼日常轻松搭茬，祝今天心情顺一点"
    elif "pet_weather" in life_hooks:
        author_state = "被猫狗日常或天气影响心情中"
        care_angle = "跟着夸一下小动物或聊聊天气，氛围感最重要"

    hooks_disp = [h for h in (*life_hooks, ctx["zh_hook"], ctx["en_hook"]) if h]
    hook_note = f" · 抓取锚点: {', '.join(hooks_disp[:3])}" if hooks_disp else ""
    return {
        "clean_body": ctx["clean_body"],
        "lang": ctx["lang"],
        "intent": intent,
        "theme": ctx["theme"],
        "life_hooks": life_hooks,
        "tech_hooks": [h for h in (ctx["zh_hook"], ctx["en_hook"]) if h],
        "author_state": author_state,
        "care_angle": care_angle,
        "drop_by_tactic": "drop_by_like_1_to_2_and_warm_reply",
        "brief_summary": f"[{author_state}] 关心点：{care_angle}{hook_note}",
    }


def _reply_candidates_for_context(ctx: dict[str, Any]) -> list[str]:
    lang = ctx["lang"]
    intent = ctx.get("intent") or "unknown"
    zh_h = ctx.get("zh_hook") or ""
    zh_h2 = ctx.get("zh_hook_2") or ""
    en_h = ctx.get("en_hook") or ""
    en_h2 = ctx.get("en_hook_2") or ""
    theme = ctx.get("theme") or "casual"
    life_hooks = set(ctx.get("life_hooks") or ())

    if intent in {"too_short", "unknown"}:
        return []

    life_front: list[str] = []
    if lang == "zh":
        if "late_night" in life_hooks or "overtime_tired" in life_hooks:
            life_front.extend([
                "都这么晚了还盯着屏幕辛苦了，这种卡住的地方往往睡一觉明早一眼就看透，今晚先歇着吧",
                "抱拳了哈哈，熬夜调这种玩意最耗心神，先合上电脑喝口水喘口气，别死磕太晚",
                "大半夜还在硬扛辛苦啦，脑子转不动的时候强撑最折磨人，早点睡保住精力要紧！",
                "加班加到这个点真挺磨人的，能吐槽出来起码还没麻木，忙完赶紧休息别内耗了",
            ])
        if "coffee_break" in life_hooks:
            life_front.extend([
                "哈哈刚准备倒杯水摸个鱼就刷到你，忙里偷闲喝一口最解压，今天别把自己逼太紧呀",
                "靠咖啡续命太真实了，看屏幕看久了就得这么偷闲缓两分钟，祝今天顺一点～",
            ])
        if "weekend_chill" in life_hooks:
            life_front.extend([
                "难得歇下来就好好放空逛逛，别老记挂电脑里那点报错了，周末开心摸鱼！",
                "周末能出门透口气挺好的，天天坐黑框前面人都快坐木了，玩开心点哈哈",
            ])
        if "pet_weather" in life_hooks:
            life_front.extend([
                "看着心情瞬间松快了不少哈哈，顺手串个门点个赞，祝今天顺心～",
                "这种天气/日常确实容易让人想偷个懒，先歇会儿刷刷推挺舒服的",
            ])
    elif lang == "en":
        if "late_night" in life_hooks or "overtime_tired" in life_hooks:
            life_front.extend([
                "oof past midnight staring at a screen is brutal, go grab some water and sleep on it — fresh eyes spot it fast",
                "honestly shutting the laptop beats fighting a stubborn bug at 1am, hope you get actual rest tonight",
            ])
        if "coffee_break" in life_hooks or "weekend_chill" in life_hooks:
            life_front.extend([
                "haha grabbing coffee and taking a breather from the screen is undefeated, hope your day treats you well",
                "good reminder to step away from the terminal for a bit, enjoy the slow scroll today",
            ])
    elif lang == "ja":
        if "late_night" in life_hooks or "overtime_tired" in life_hooks:
            life_front.extend([
                "遅くまでほんとお疲れさまです…今日は無理せず切り上げてゆっくり休んでくださいね",
            ])
        if "coffee_break" in life_hooks or "weekend_chill" in life_hooks:
            life_front.extend([
                "息抜きしつつゆるく過ごすのちょうどいいですね〜今日もマイペースでいきましょ",
            ])

    if intent == "notif_follow":
        if lang == "ja":
            return [
                "フォローありがとうございます！普段はコード書いたりタイムラインでゆるく雑談したりしてます、よろしくお願いします〜",
                "フォロバ感謝です！たまに見かけて気になった投稿にゆるく絡んでます、よろしくどうぞ！",
                "つながりありがとうございます！気ままに呟いたり絡んだりしてるので今後ともよろしくお願いします〜",
            ]
        if lang == "ko":
            return [
                "맞팔 감사합니다! 평소엔 코딩하다가 타임라인 구경하며 가볍게 수다 떠는 편이에요, 잘 부탁드립니다ㅎㅎ",
                "팔로우 고맙습니다! 지나가다 자주 들러서 가볍게 안부 남길게요, 오늘도 좋은 하루 보내세요~",
            ]
        if lang == "zh":
            return [
                "谢回关呀！平时也就写写代码摸鱼上来瞎聊两句，刷到了顺手打个招呼，后面常来串门哈哈",
                "看到回关啦～平时对着电脑写烦了就上 X 找真人唠嗑吐槽，先混个脸熟，有空多聊！",
                "哈哈接住回关！看你最近动态挺有意思的，后面刷到经常顺手点个赞打招呼～",
                "谢谢关注呀，X 上碰到能正常随口聊几句的活人朋友最舒服，祝最近天天好心情！",
            ]
        return [
            "appreciate the follow! usually coding all day and dropping by here whenever i need a breather lol",
            "thanks for connecting! always down to trade casual dev rants and timeline banter",
            "glad we crossed paths on the feed! hope your week is going smooth, see you around",
        ]

    if intent == "milestone":
        if lang == "ja":
            return [
                "達成おめでとうございます！タイムラインでいろんな人とゆるく絡めるの楽しいですよね、これからもよろしくです！",
                "おお、おめでとうございます！マイペースに発信を続けてここまで伸ばしてるのすごいですね。",
                "節目の達成おめでとうございます！これからもタイムラインで見かけたら気軽に絡ませてください〜",
            ]
        if lang == "zh":
            return [
                "恭喜破圈！在 X 上能多遇到几个正常聊天的活人朋友挺难得的，替你开心，继续冲哈哈",
                "恭喜呀！写到后面发现还是跟真活人瞎聊几句最有意思，祝早日再翻倍～",
                "看你一路坚持下来终于到这个节点啦，真诚发内容真的会慢慢聚起同好，恭喜恭喜！",
            ]
        return [
            "congrats on hitting the milestone! always fun running into real humans on the feed",
            "nice milestone! keeping things genuine here beats chasing empty numbers every time lol",
            "huge congrats on the growth! awesome seeing steady real posting actually pay off",
        ]

    if intent == "creative":
        if lang == "ja":
            return [
                "執筆おつかれさまです！創作続けながらここまで伸ばしてるの純粋にすごいですね、無理せず頑張ってください！",
                "更新お疲れさまです！合間に息抜きしつつ、これからも楽しく書き続けられるよう応援してます〜",
            ]
        if lang == "zh":
            return [
                "能一直保持创作输出真心厉害，比我天天盯着黑框终端看代码浪漫多了哈哈，别太累注意休息！",
                "看着好有生活气息呀，顺手点个赞留个爪，保持这个节奏慢慢画/写下去～",
            ]
        return [
            "love seeing folks actually shipping creative work on here, keep going at your own pace!",
        ]

    if intent == "language_learning":
        if lang == "zh":
            return [
                "确实，学语言三天不读嘴就瓢了，还是得天天泡时间线里跟人瞎聊最管用哈哈",
                "太对了我刷 X 一半原因也是硬逼自己多看多回几句外文，比背单词软件管用多了",
                "真实！以前背一堆语法转身就忘，反而是每天上来看看大家怎么吐槽记得最牢哈哈",
            ]
        return [
            "honestly scrolling timelines and chatting with people beats flashcards every single time lol",
        ]

    if intent == "vent_rant":
        if lang == "ja":
            return life_front + [
                "ほんとそれ疲れますよね…今日は無理に粘らず早めに切り上げて休んだ方が明日の効率良さそうです。",
                "わかる、夜遅くまで粘るほど余計に泥沼ハマりがちなので一旦コーヒー飲んで離れるの正解です笑",
            ]
        if lang == "ko":
            return [
                "진짜 그럴 땐 억지로 붙잡고 있는 것보다 물 한 잔 마시고 잠깐 쉬는 게 훨씬 낫더라고요, 오늘 고생 많으셨어요!",
            ]
        if lang == "zh":
            return life_front + [
                "草，太真实了，越急着收尾越容易卡在离谱的地方……实在不行先关电脑喝口水休息下，明早一眼就能看出来",
                "太懂了，写东西最搞心态的就是眼看要收工突然给你整波大活，先喘口气别气着自己哈哈",
                "有些破问题盯着死磕半小时不如出去溜达一圈两分钟看透，保住好心情和头发要紧！",
                "真实到想笑又想叹气……能吐槽出来说明还没被磨麻木，今晚别再内耗啦",
                "哈哈同款日常，顺的时候丝滑得飞起，卡住的时候直接把人脑子干烧，先放一放明天再战",
            ]
        return life_front + [
            "honestly stepping away for 20 mins beats staring angrily at the screen every single time lol",
            "ugh too real, things go super smooth until one random edge case decides to ruin your evening",
            "rough stretch there — hope things smooth out soon and you get a real break after this",
        ]

    if intent == "social_chatter":
        if lang == "ja":
            return life_front + [
                "タイムライン流れてきて思わずお邪魔しました！今日もゆるくやっていきましょ〜",
                "わかります、パソコンの前で煮詰まったときほどタイムライン眺めたくなりますよね笑",
                "ふらっと立ち寄りました〜無理しすぎずマイペースにいきましょう！",
            ]
        if lang == "ko":
            return [
                "타임라인 보다가 반가워서 슬쩍 인사 남기고 가요! 오늘도 편하게 하루 보내세요ㅎㅎ",
            ]
        if lang == "zh":
            return life_front + [
                "路过你主页顺手串个门混个脸熟～平时写完东西脑子发木就爱逛推找活人扯两句，顺手点个赞",
                "刚从电脑前抬头透口气，X 上还是得跟真人闲聊吐槽才有烟火气哈哈，今天还顺利吗",
                "同款网上冲浪选手路过留名～天天看终端烦了就出来透透气，祝今天开心顺意！",
                "哈哈活人浓度挺高呀，刚好忙完一阵上来逛一圈，常在时间线串门见面～",
                "刷到这条莫名亲切，先赞为敬！有空出来喝杯水歇歇脚，别一直对着屏幕呀",
                "平时大家都在各自忙，能在时间线上凑巧碰上随口搭两句还挺温暖的哈哈",
            ]
        return life_front + [
            "just taking a quick break from staring at a terminal all day, good running into actual humans here lol",
            "hey! scrolling the feed with some coffee before getting back to coding, hope your week's going alright",
            "dropping by your timeline to say hey — nice seeing genuine folks hanging out instead of promo bots",
        ]

    # intent == "tech_discussion": empathetic peer-to-peer casual coder banter (NO corporate PPT tone).
    if lang == "ja":
        jp_tech = list(life_front)
        if en_h:
            jp_tech.append(f"{en_h}まわりって動かすまでは早いのに、例外と再試行の穴埋めが地味に時間溶けますよね笑 お疲れさまです")
        return jp_tech + [
            "ほんとそれです、ハッピーパス動いてからの例外処理まわりが一番時間溶けますよね、無理せず休憩挟んでください",
            "実環境で一晩回すとローカルのテストでは絶対出ない競合が急に顔出すのあるあるすぎます",
            "コード書いた直後は完璧に見えるのに翌日ログ見ると別世界なの毎回笑うしかないです",
            "変に凝った仕組みより地味だけど後から追える作りにしておくと助かる場面多いですよね〜",
            "DOMとか画面側の変化に振り回されるの分かりみ深いです、再確認挟むだけでだいぶ落ち着きますよね",
        ]
    if lang == "ko":
        ko_tech = []
        if en_h:
            ko_tech.append(f"{en_h} 쪽은 처음 붙일 땐 금방 되는데, 예외 복구랑 타이밍 꼬이는 거 막다가 하루 다 가죠ㅋㅋ 고생 많으십니다")
        return ko_tech + [
            "진짜 공감돼요, 정상 케이스는 10분이면 되는데 엣지 케이스 막다가 주말 다 가는 느낌ㅋㅋ",
            "로컬에선 멀쩡하던 게 실제 환경에서 밤새 돌려놓으면 꼭 이상한 데서 터지더라고요",
            "자동화 짜다 보면 기능 추가보다 중간에 조용히 씹히는 상태 잡는 게 제일 일인 듯요",
            "돌아가는 데모 만드는 건 빠른데 안 죽고 오래 버티게 다듬는 게 진짜 체력전 맞죠ㅎㅎ 잠깐 쉬엄쉬엄 하세요",
        ]
    if lang == "zh":
        anchor_zh = []
        if zh_h:
            anchor_zh = [
                f"讲真搞{zh_h}跑通快乐路径十来分钟的事，剩下两天全在堵奇奇怪怪的边界和超时，辛苦了哈哈",
                f"同踩过{zh_h}的坑，最折腾人的不是直接报错，而是悄咪咪吞掉失败还以为成了，排查起来太费神",
                f"折腾{zh_h}折腾久了真深有体会，外围逻辑越简朴半夜睡觉越安心，搞完记得喝口水歇会儿",
            ]
        theme_zh = {
            "concurrency_state": [
                "并发最搞心态的真不是加锁，是校验和写入没包一起，报错卡半空最难捞，调通这波辛苦啦",
                "状态机这东西平时觉得啰嗦，一旦半夜跑批出幺蛾子，能不能干净停下来全靠它续命",
            ],
            "browser_dom": [
                "页面自动化千万别信死等几秒或瞎点坐标，前端稍微换个布局当场抓瞎，盯紧 DOM 刷新核对省心太多",
                "写浏览器驱动最烦就是页面刚跳转节点还没出来，读个空列表回来瞬间高血压哈哈",
            ],
            "rate_pacing": [
                "写过调度器之后真不敢硬堆并发了，慢一点拉开间隔稳跑几天，比冲五分钟吃 429 舒心十倍",
                "遇到连续不明状态能果断停手真的很冷静，硬顶重试通常只会把自己心态搞炸",
            ],
            "perf_benchmark": [
                "看评测只看均值真的太容易被骗，我每次必翻 p99 长尾，真卡脖子的全躲在角落里",
                "性能调到底十有八九不是算法太慢，是某处傻傻空等超时拖垮整条线哈哈，查出来那一刻巨爽",
            ],
            "agent_tooling": [
                "写 MCP 和 Agent 工具最怕上行模型路由抽风，把规则和状态守卫放到本地稳妥太多了",
                "工具报错如果不把下一步怎么改写明白，看着 Agent 在那死循环把额度烧干真的会笑死",
            ],
        }.get(theme, [])
        return life_front + anchor_zh + theme_zh + [
            "哈哈写代码前自信满满觉得三行搞定，一跑真实环境当场教做人，搞定完早点歇着呀",
            "本地 mock 全绿的时候千万别飘，真流量跑两圈各种离奇边界全出来了，太理解这过程有多磨人",
            "独立做点小工具全靠一口气磨细节，跑顺那一刻还是超有成就感的，别忘喝水休息～",
            "大半夜调试最绝望的莫过于日志干干净净但结果不对，只能打断点一点点摸，实在困了明早再看",
            "赞同！自己折腾出来的工具简单稳当比花哨靠谱多了，平时不用天天提心吊胆盯着日志",
            "同款天天跟 bug 搏斗选手路过串个门～修完这处顺手去喝口茶喘口气吧",
            "看你一点点把坑填平分享出来真挺实在的，比看那些飘在天上的教程解压多了",
            "有的时候卡半天以为是玄学问题，最后查出来是个换行或者端口小细节，找着以后又好气又好笑",
            "越折腾越觉得能稳稳跑过夜的小脚本才是最香的，平时还能多留点时间摸鱼睡觉哈哈",
        ]
    # English relaxed, empathetic tech coder banter
    anchor_en = []
    if en_h:
        anchor_en = [
            f"honestly {en_h} takes like 10 mins for the happy path and then two days chasing weird edge cases lol, hang in there",
            f"been tinkering with {en_h} too lately — silent failures drain way more energy than loud crashes",
            f"yeah {en_h} always looks breezy until real overnight runs trip over race conditions, hope the fix holds cleanly",
        ]
    theme_en = {
        "concurrency_state": [
            "nothing humbles you faster than partial writes when two workers race before a lock clears — glad you caught it",
            "keeping history append-only and checking invariants before mutating anything saved me so many messy rollbacks",
        ],
        "browser_dom": [
            "totally, DOM selectors plus a quick reload verify beat guessing coordinates the second a feed layout shifts",
            "navigation commit firing before actual timeline nodes render has fooled my scripts more times than i want to admit",
        ],
        "rate_pacing": [
            "spacing things out and backing off fast on 429s beats hammering retries every single time",
            "halting immediately after two weird errors feels overly paranoid until it saves a whole account haha",
        ],
        "perf_benchmark": [
            "watching p99 and truncated errors tells the real story, averages lie constantly under burst load",
            "half the latency wins i ever got came from ripping out pointless polling instead of micro-optimizing code",
        ],
        "agent_tooling": [
            "keeping the client wrapper thin and validating state server-side saved me from so much model routing chaos",
            "one tiny integer schema bound out of range can randomly 500 a route while everything else looks fine lol",
        ],
    }.get(theme, [])
    base_en = [
        "haha local mocks always pass until real traffic exposes timing races in five minutes flat",
        "zero-guesswork state checks feel slow at first till you stop waking up to clean broken runs",
        "honestly boring deterministic logic beats clever heuristics whenever you cant roll back an action",
        "most automation breaks in the tiny glue code between tools, never in the happy path itself",
        "lol classic weekend dev story: thought it was a two-line tweak and suddenly you're rewriting locks",
        "nothing better than finally spotting a tiny bug after staring at logs for two hours, pure relief — go grab a break",
        "real-world sessions always find the one weird case no unit test ever thought to mock",
        "keeping error payloads readable is underrated until 1am debugging hits, take care of your sleep too",
        "small focused checks right next to the bug you just hit always age way better than giant rewrites",
        "simple predictable scripts age way better than clever black boxes every time",
        "always appreciate real builders sharing the messy parts out in the open, hope your weekend stays bug-free",
        "haha nothing teaches defensive coding faster than watching your own script touch a live page",
    ]
    if en_h2 and en_h2 != en_h:
        base_en.insert(0, f"combining {en_h} with clean {en_h2} checks makes overnight runs way less stressful")
    return life_front + anchor_en + theme_en + base_en


def get_casual_interaction_templates() -> dict[str, Any]:
    """Return pre-drafted, non-AI casual drop-by interaction templates for early-stage account growth."""
    return {
        "purpose": "前期主动串门、先分析对方推文语气与处境、再带关心口吻随意闲聊，绝对不用 AI 套话。",
        "early_drop_by_tactics": [
            "1. 先逛对方主页（x_visit_profile）看 Bio 和最近 2–3 条推文，不要看到帖子就冷冰冰发评论或直接关注。",
            "2. 对刚认识的同好实行『串门 1–2 赞 + 1 句自然关心的留言』，同一波对同一作者最多回 1 条，绝不刷屏。",
            "3. 优先挑 20–5000 粉、活跃度正常且当前评论数 <=5 的真人推文留言，留言会被作者真正在通知栏看到，回复/回关率最高。",
            "4. 评论前先读懂对方此刻状态（加班累了/刚上线开心/晒猫喝咖啡/调试卡住），先共情关心，再顺着正文具体词接一句。",
            "5. 遇到仅有 2–4 字且无上下文的短句（如「咋了」），或没看懂的生活观察，宁可只点赞不硬聊，防止机器人翻车。",
        ],
        "scenarios": [
            {
                "id": "late_night_overtime",
                "title": "1. 对方加班 / 熬夜 / 吐槽破防（先关心安抚）",
                "analyze_cue": "推文里出现：凌晨、半夜、加班、好累、失眠、报错破防、burnout、1am。",
                "care_focus": "不要急着当老师给建议，先替对方心疼一句、叮嘱喝水早点休息。",
                "templates_zh": [
                    "都这么晚了还盯着屏幕辛苦了，这种卡住的地方往往睡一觉明早一眼就看透，今晚先歇着吧",
                    "抱拳了哈哈，熬夜调这种玩意最耗心神，先合上电脑喝口水喘口气，别死磕太晚",
                    "加班加到这个点真挺磨人的，能吐槽出来起码还没麻木，忙完赶紧休息别内耗了",
                ],
                "templates_en": [
                    "oof past midnight debugging is brutal, go grab some water and sleep on it — fresh eyes spot it in two mins",
                    "honestly shutting the laptop beats fighting a stubborn edge case at 1am, hope you get real rest tonight",
                ],
                "templates_ja": [
                    "遅くまでほんとお疲れさまです…今日は無理せず切り上げてゆっくり休んでくださいね",
                ],
                "avoid": "禁止说「实战踩坑值得总结」「建议重构状态机」「从工程角度看」",
            },
            {
                "id": "casual_drop_by_life",
                "title": "2. 对方喝咖啡 / 摸鱼 / 周末出摊交友（纯日常随意闲聊）",
                "analyze_cue": "推文里出现：出摊、活人、咖啡、冰美式、周末、摸鱼、天气、猫狗日常。",
                "care_focus": "展示自己也是有血有肉的摸鱼活人，顺口夸一杯咖啡/祝周末开心，顺便混个脸熟。",
                "templates_zh": [
                    "路过你主页顺手串个门混个脸熟～平时写完东西脑子发木就爱逛推找活人扯两句，顺手点个赞",
                    "哈哈刚准备倒杯水摸个鱼就刷到你，忙里偷闲喝一口最解压，今天别把自己逼太紧呀",
                    "难得闲下来就好好放空逛逛，别老记挂电脑里那点报错了，周末开心摸鱼！",
                ],
                "templates_en": [
                    "just taking a quick break from staring at a terminal all day, good running into actual humans here lol",
                    "dropping by your timeline to say hey — nice seeing genuine folks hanging out instead of promo bots",
                ],
                "templates_ja": [
                    "タイムライン流れてきて思わずお邪魔しました！今日もマイペースにいきましょ〜",
                ],
                "avoid": "禁止说「同路人常联系」「留个脚印关注」「很高兴认识优秀的你」",
            },
            {
                "id": "reciprocal_followback",
                "title": "3. 对方刚回关 / 刚在通知里互动（趁热打招呼）",
                "analyze_cue": "通知事件 follow / reply / mention，对方刚刚点进你主页看了。",
                "care_focus": "轻松接住善意、一句话交代自己平时都在发什么/爱闲聊，不搞广告推销。",
                "templates_zh": [
                    "谢回关呀！平时也就写写代码摸鱼上来瞎聊两句，刷到了顺手打个招呼，后面常来串门哈哈",
                    "看到回关啦～平时对着电脑写烦了就上 X 找真人唠嗑吐槽，先混个脸熟，有空多聊！",
                ],
                "templates_en": [
                    "appreciate the follow! usually coding all day and dropping by here whenever i need a breather lol",
                    "glad we crossed paths on the feed! hope your week is going smooth, see you around",
                ],
                "templates_ja": [
                    "フォローありがとうございます！普段はコード書いたりタイムラインでゆるく雑談したりしてます〜",
                ],
                "avoid": "禁止发链接、禁止一上来推销产品或请求互赞三连",
            },
            {
                "id": "milestone_celebration",
                "title": "4. 对方破百破千粉 / 项目上线小节点（真诚祝贺）",
                "analyze_cue": "推文里出现：破百、破千、フォロワー達成、followers、shipped、launched。",
                "care_focus": "肯定对方一路坚持下来的不容易，像朋友一样随口替他开心。",
                "templates_zh": [
                    "恭喜破圈！在 X 上能多遇到几个正常聊天的活人朋友挺难得的，替你开心，继续冲哈哈",
                    "看你一路坚持下来终于到这个节点啦，真诚发内容真的会慢慢聚起同好，恭喜恭喜！",
                ],
                "templates_en": [
                    "congrats on hitting the milestone! always fun running into real humans on the feed",
                ],
                "templates_ja": [
                    "達成おめでとうございます！マイペースに発信を続けてここまで伸ばしてるのすごいですね。",
                ],
                "avoid": "禁止「流量密码」「稳步往上走」「值得借鉴的增长方法论」",
            },
            {
                "id": "tech_peer_empathy",
                "title": "5. 对方写代码 / 调工具卡坑（同行体谅 + 具体细节共鸣）",
                "analyze_cue": "正文出现具体技术词（MCP、Rust、Playwright、并发、429、部署等）且作者明显被折腾到。",
                "care_focus": "先体谅调试真耗时、提醒劳逸结合，再带上对方推文里的具体技术名词轻聊一句。",
                "templates_zh": [
                    "同款天天跟 bug 搏斗选手路过串个门～修完这处顺手去喝口茶喘口气吧",
                    "讲真跑通快乐路径十来分钟的事，剩下两天全在堵奇奇怪怪的边界和超时，辛苦了哈哈",
                ],
                "templates_en": [
                    "haha local mocks always pass until real traffic exposes timing races in five mins — hope the fix sticks cleanly",
                ],
                "avoid": "禁止用双破折号「——」做 PPT 总结，禁止说教「建议你采用某某架构」",
            },
            {
                "id": "skip_reply_like_only",
                "title": "6. 仅点赞不硬回的边界场景（保底不翻车）",
                "analyze_cue": "超短句（如「咋了」「？」）、无上下文碎碎念、或完全看不懂语境的内容。",
                "care_focus": "给一个点赞就是最好、最不尴尬的礼貌存在感，绝不编造理解去硬回。",
                "templates_zh": ["[系统规则：fail-closed 返回 None -> 仅点 1 赞，不输出评论]"],
                "templates_en": ["[Fail-closed: return None -> like only, zero fabricated reply]"],
                "avoid": "禁止强行套用万能模板（否则出现「收到你的补充…」级社死翻车）",
            },
        ],
        "banned_ai_phrases_examples": list(_AI_CLICHE_PATTERNS),
    }


_VARIED_REPLY_POOLS: dict[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    ("zh", "tech_discussion"): (
        (
            "写脚本最怕遇到，",
            "小工具开发真是，",
            "刚摸鱼刷到这段，",
            "同为夜里盯日志，",
            "看你调这部分时，",
            "实操起来常发现，",
            "讲白了真动手后，",
            "自己折腾这玩意，",
            "串门逛到你主页，",
            "哈哈刚停下手头活，",
            "跑通测试那一刻，",
            "改代码老碰上这，",
            "平时写自动化时，",
            "真被这细节磨过，",
            "顺手点赞搭句话，",
            "看得出自己敲过，",
            "摸索这类工具时，",
            "熬过初期调试后，",
            "太有同感了老哥，",
            "盯了一天黑框后，",
            "越往底层越觉得，",
            "做个人项目最懂，",
            "刚顺完本地逻辑，",
            "刷时间线看到这，",
        ),
        (
            "开心流程十分钟写完，剩余两天都在堵奇葩超时",
            "最消耗人的不是当场红字，是悄悄吞错让你误判",
            "外围保护越简朴老实，夜里睡得才越踏实安心",
            "步子放稳少抢几秒，比猛冲五分钟吃限流香太多",
            "盯死半小时理不清时，去倒杯温水回来一眼认出",
            "接口文档写得天花乱坠，实际全看凌晨实机脾气",
            "能按预期安安静静跑完一宿，比啥花哨界面都强",
            "页面结构微微动一下，还是加一层复核最能救命",
            "出错说明写成直白大白话，三天后再查能少掉头发",
            "本地假数据全绿别飘，上线跑几轮才是真正考验",
            "每次以为终于收工，角落总有个边界条件冒出来",
            "看似多余的那几行检查，出事全靠它们帮忙兜底",
            "比起推翻整套重写，在痛点旁边加道护栏更管用",
            "一步步看清实际页面状态，比盲猜几秒靠谱十倍",
            "把日志打得清爽干练些，排查起来血压都没那么高",
            "越是不起眼的小开关，卡起人来往往最让人头疼",
            "独自磨顺整条链路确实累，跑起来那下真挺解压",
            "别贪快一顿猛操作，稳扎稳打反而最少返工重来",
            "真正稳定省心的工具，往往长得最平平无奇朴素",
            "看似顺滑的表象底下，十有八九藏着重试死结",
            "别被五分钟原型骗了，边界保护才占九成精力",
            "少堆几个花哨参数，夜里报警短信都能少一半",
            "挑对安全速度慢跑，比硬飙高并发少掉一半坑",
            "把状态变化记清楚些，真出岔子三秒定位源头",
        ),
        (
            "，先留个赞，改完喝口茶歇歇～",
            "，顺手串个门，别盯屏幕太久呀",
            "，辛苦啦，弄完早点去休息哈哈",
            "，赞一个，有空时间线继续瞎聊",
            "，哈哈多注意护眼，祝今天顺利！",
            "，混个脸熟，愿接下来一路绿灯",
            "，点杯冰美式歇口气再接着搞～",
            "，顺滑收工记得早点合盖睡觉哦",
            "，路过留爪，后面有新发现继续唠",
            "，别太拼啦，稳稳推进最有底气",
            "，歇会儿啃点零食再接着写",
            "，先去倒杯温水放松下眼睛",
            "，哈哈同路调码人加油顶住",
            "，晚点弄顺记得早点关电脑",
            "，赞完继续逛时间线找活人",
            "，希望这周测试全部一遍过",
            "，抽空出来摸会儿鱼喘口气",
            "，稳住心态明天一切好说～",
        ),
    ),
    ("en", "tech_discussion"): (
        (
            "funny tho ",
            "lately, ",
            "real talk ",
            "honestly ",
            "dev mood: ",
            "wild part ",
            "odd bug: ",
            "pro tip: ",
            "2am note: ",
            "by the way ",
            "side note ",
            "tiny tip: ",
            "code life ",
            "2am code: ",
            "fresh eye ",
            "localhost ",
            "peak dev: ",
            "hot take: ",
            "tea break ",
            "my tools: ",
            "weekend: ",
            "ugh haha, ",
            "true tho, ",
            "one catch ",
        ),
        (
            "clean prototypes fly fast till real latency peeks in",
            "silent skips cost way more sleep than loud tracebacks",
            "simple state checks beat clever black boxes at 2am",
            "steady human pacing keeps runs healthy way longer",
            "walking away for water spots the typo in seconds",
            "live endpoints always invent one case mocks missed",
            "plain error strings save hours of groggy debugging",
            "UI layout shifts love breaking brittle blind delays",
            "small targeted fixes age better than big rewrites",
            "rechecking DOM state right after clicks kills ghosts",
            "overnight runs expose every sneaky lock assumption",
            "one tiny edge timeout drags down the whole pipeline",
            "boring reliable glue handles messy feeds best",
            "trimming pointless polling cuts half the weird bugs",
            "catching partial writes early prevents rough mornings",
            "watching your script touch a real tab humbles fast",
            "simple step bounds turn scary rollouts into calm naps",
            "keeping wrappers minimal leaves less to babysit",
            "calm explicit retries beat silent fallback guesses",
            "short cool timeouts save whole batches from stalling",
            "clean logs turn midnight panics into quick edits",
            "testing live browser clicks beats ten fake unit runs",
            "steady rhythm keeps live runs healthy past week one",
            "tiny glue bugs always wait till Friday night to wave",
        ),
        (
            ", hope it behaves cleanly!",
            ", go grab some water now",
            " lol, hang in there today",
            ", wishing you smooth runs",
            ", dropping a quick like!",
            ", rest your eyes soon too",
            ", good luck shipping this",
            ", see you around the feed",
            ", take a breather after",
            " haha, smooth sailing ahead",
            ", grab a warm coffee soon",
            " lol, may tests stay green",
            ", sending good debug luck!",
            ", shut the lid early tonight",
            ", cheering from the timeline",
            ", take five and stretch a bit",
            ", calm vibes rest of week",
            " haha, hope this wraps fast",
        ),
    ),
    ("zh", "vent_rant"): (
        (
            "唉真不容易，",
            "抱拳了老兄，",
            "刷到这条懂了，",
            "忙一半卡壳，",
            "隔屏都觉得累，",
            "熬这么晚真拼，",
            "顺手赞你打气，",
            "破事越急越乱，",
            "能吐槽挺好，",
            "电脑前久了嘛，",
            "哈哈太真实了，",
            "碰上这事头大，",
            "连轴转确实熬，",
            "先喘口气吧，",
            "辛苦打工人，",
            "刚歇脚看见你，",
        ),
        (
            "盯着屏幕死磕半小时不如合盖去溜达两分钟",
            "脑子发木的时候强行改往往越改越乱套",
            "好多莫名其妙的毛病睡一觉明早一眼看透",
            "千万别把所有烦躁和火气都往自己身上揽",
            "平时拼得太紧偶尔掉个链子也再正常不过",
            "去倒杯温水或者吃点小零食换换脑子好得快",
            "剩下的烂摊子留给清醒的自己明早再来收",
            "今天能撑到现在已经很了不起真别苛责",
            "手头杂事哪天也做不完但好心情得先保住",
            "越着急收尾越容易被小卡点绊住脚歇会儿",
            "找个朋友吐槽两句把闷气倒出来轻松多啦",
            "眼睛酸了就赶紧闭目养神十来分钟最实在",
        ),
        (
            "，今晚早点歇着别内耗啦",
            "，休息好再战别委屈自己～",
            "，喝口水缓缓祝明天顺意",
            "，睡个整觉明天肯定顺得多",
            "，实在困了赶紧躺平去吧",
            "，缓口气慢慢来都会好的",
            "，串门留句暖话陪你透口气～",
            "，照顾好身体比赶这一会儿重要",
        ),
    ),
    ("en", "vent_rant"): (
        (
            "ugh sorry ",
            "man oof, ",
            "rough day ",
            "breathe, ",
            "oof mood, ",
            "hang in, ",
            "stopping, ",
            "brutal, ",
            "steep day ",
            "send tea, ",
            "zero fun, ",
            "wild day, ",
            "seriously ",
            "tough run ",
            "honest: ",
            "oof lol, ",
        ),
        (
            "closing the laptop beats fighting tired mistakes at night",
            "staring angry at a screen never fixed a weird glitch faster",
            "stepping outside for ten minutes resets half the headache",
            "fresh morning eyes spot in two minutes what midnight hides",
            "nobody owes clean perfection when things keep shifting fast",
            "venting it out once takes half the sting right away",
            "drinking real water and eating dinner solves way more",
            "leaving half-broken clutter till tomorrow is underrated",
            "pacing yourself saves energy for stuff that actually matters",
            "weird days hit everyone once in a while so go easy tonight",
            "taking five minutes away from notifications clears your head",
            "getting decent sleep tonight outperforms grinding till 2am",
        ),
        (
            ", hope tonight treats you gently",
            ", get some proper rest soon!",
            ", tomorrow starts way cleaner",
            ", rooting for an easy morning",
            ", dropping a warm like here",
            ", treat yourself after this",
            ", take care of your sleep too",
            ", hope things calm down quick",
        ),
    ),
    ("zh", "social_chatter"): (
        (
            "刚抬头刷推，",
            "午后倒水歇脚，",
            "写累出来逛，",
            "公屏全广告啊，",
            "串门打个招呼，",
            "摸会儿鱼冒泡，",
            "看你这挺生活，",
            "忙完上来透风，",
            "懒得看黑框了，",
            "同款冲浪路过，",
            "哈哈凑个热闹，",
            "溜达逛进主页，",
            "顺手刷到这条，",
            "平时爱逛真人，",
            "抽空闲聊两句，",
            "茶水间摸鱼中，",
        ),
        (
            "看到真活人随便发点日常莫名觉得轻松好多",
            "先点个赞混个脸熟后面时间线碰上继续聊呀",
            "忙里偷闲上来扯两句瞎话才像正常的社交圈",
            "希望你今天不管忙工作还是休息都顺顺心心",
            "泡杯喜欢的饮料稍微发会呆放空真的超级舒服",
            "能遇上说话接地气不端着的朋友真的挺难得的",
            "先别总记挂待办清单里那堆没完没了的小事",
            "偶尔放慢节奏在主页溜达一圈反而觉得挺解压",
            "感觉时间线上还是得多留点这样有烟火气的话",
            "大伙各自忙活之余能顺口搭句话还怪温暖的",
            "屏幕看多了出来换换眼睛顺手交个网友真不赖",
            "难得碰上同频闲聊的人多半天心情都会变好呢",
        ),
        (
            "，祝今天开心顺意～",
            "，后面常来你主页串门哈哈",
            "，别太累记得按时吃饭喝水",
            "，先赞为敬有空多唠嗑～",
            "，祝好心情延续一整天呀",
            "，回头有好玩的事继续聊！",
            "，慢慢享受这会儿摸鱼时光吧",
            "，逛下一家去啦祝一切顺利",
        ),
    ),
    ("en", "social_chatter"): (
        (
            "hey lol, ",
            "tea stop, ",
            "scrollin, ",
            "chill day ",
            "casual hi ",
            "take five ",
            "nice one, ",
            "hey feed, ",
            "fresh air ",
            "good vibe ",
            "lazy feed ",
            "hi again, ",
            "real ppl, ",
            "off code, ",
            "weekend, ",
            "slow day, ",
        ),
        (
            "actually chatting with genuine humans beats promo feeds",
            "stepping away from a dark terminal makes scrolling nice",
            "enjoying slow random timeline conversations today for once",
            "dropping a friendly like while waiting for tea to cool down",
            "normal everyday posts make the whole feed feel alive again",
            "catching a quick breather before diving back into chores",
            "funny how five casual replies brighten an average workday",
            "love running into real voices not pushing hype threads",
            "hope your inbox stays quiet and your drinks stay cold",
            "taking time to hang out without an agenda feels great",
            "slow afternoons scrolling chill timelines hit the spot",
            "wishing more feeds stayed this relaxed and down to earth",
        ),
        (
            ", hope your day runs smooth!",
            ", catch you around on here",
            " haha, enjoy the slow pace",
            ", good vibes to you today",
            ", always fun crossing paths",
            ", take care and stay rested",
            ", see you next coffee break",
            ", wishing you a calm week!",
        ),
    ),
    ("zh", "notif_follow"): (
        ("谢回关呀，", "看到关注啦，", "欢迎串门呀，", "接住回关咯，", "哈喽新朋友，", "刚看通知啦，", "真巧碰上了，", "很高兴认识，"),
        ("平时写写小工具累了就爱上来找真人瞎扯两句", "大多时候坐在电脑前捣鼓代码顺手逛逛时间线", "看你日常发的东西挺有生活气息我也爱随便聊聊", "能碰到不整虚头巴脑的同好真开心先混个脸熟呀", "我这边平常也就吐吐槽分享点边角杂记没啥包袱"),
        ("，后面常来互相点赞串门～", "，祝今天顺心天天开心哈哈", "，有空继续在时间线唠嗑呀！", "，先赞为敬以后多照应～"),
    ),
    ("en", "notif_follow"): (
        ("thanks for the add, ", "appreciate connecting! ", "hey nice follow, ", "glad to cross paths, ", "cool to see you, ", "welcome aboard lol "),
        ("mostly coding daily and hopping on here whenever i need air", "love trading casual timeline chatter instead of stiff promo talk", "saw your recent notes and dropped by to say a quick hello", "always down for laid-back conversations and small wins"),
        (", catch you around!", " — hope your week goes great", ", see you on the feed soon!", " lol, stay cool out there"),
    ),
    ("zh", "milestone"): (
        ("恭喜达成呀，", "太赞了哈哈，", "看着真开心，", "真不错呀老兄，", "替你高兴呢，", "一路坚持下来，", "这节点太稳了，", "小庆祝一下，"),
        ("认真按自己节奏写内容还是最能聚起同频活人朋友", "不急不躁坚持输出居然不知不觉攒下这么多同好了", "在满屏吵闹的时间线里遇到真实交朋友的博主挺难得", "大家隔着屏幕能经常顺口聊两句真的比看空数字舒服"),
        ("，继续按喜欢的方式冲呀～", "，祝下一个小目标更快实现！", "，点个赞一起开心开心哈哈", "，后续常串门捧场哦～"),
    ),
    ("en", "milestone"): (
        ("huge congrats here! ", "awesome milestone, ", "love seeing this win, ", "cheers to that! ", "really neat progress, ", "way to stick with it, "),
        ("steady real voice always finds genuinely fun people over time", "nice seeing consistent everyday posting build a real circle", "actual conversations beat empty vanity metrics every single week", "cool watching small genuine steps compound quietly on here"),
        (", keep enjoying the ride!", " — wishing many more ahead", ", well deserved break tonight", " lol, cheering from the feed"),
    ),
    ("zh", "creative"): (
        ("写画得真有味道，", "好喜欢这感觉，", "坚持创作超厉害，", "顺手驻足点赞，", "路过被氛围留住，", "慢工出细活呀，", "很有自己节奏，", "看得挺放松的，"),
        ("天天对着枯燥电脑框之后刷到这种温柔日常超治愈", "愿意按自己步调一点点磨出完整作品真的特别难得", "每次在时间线遇到认真搞创作的朋友都会停下来多看两眼", "不追热点安静把故事和画面写好本身就很有力量啦"),
        ("，别累着注意喝水休息呀～", "，慢慢更新会一直留赞捧场！", "，祝创作灵感满满心情好～", "，有新作再来看哈哈"),
    ),
    ("en", "creative"): (
        ("really love the mood here, ", "super nice seeing real art, ", "awesome creative flow, ", "stopped scrolling for this: ", "genuine craft always shines, "),
        ("making your own quiet work steadily beats noisy feed trends", "cool contrast stepping out of terminal windows into fresh visuals", "keeping a calm personal rhythm produces way better stories", "nice seeing people still pour heart into things they enjoy"),
        (", keep going at your pace!", " — hope inspiration flows easy", ", dropping a warm like here", ", take breaks between pieces!"),
    ),
    ("zh", "language_learning"): (
        ("太有同感啦，", "确实是这样，", "学语言真得靠泡，", "深有体会哈哈，", "说得太实在了，", "同款练语感方式，"),
        ("死背软件卡片过两天全还给书本了还是直接逛推看真人吐槽记得牢", "每天在时间线多看多顺口接两句比干啃厚字典管用十倍不止", "其实语感全靠在轻松闲聊里一点点撞出来根本不用背得苦大仇深", "碰上有趣的小段子顺手回上一两句反而记一辈子单词"),
        ("，咱们多在时间线瞎聊练语感～", "，祝越聊越顺手不卡壳！", "，先赞一个每天进步一点点哈哈", "，继续轻松泡着学准没错"),
    ),
    ("en", "language_learning"): (
        ("totally true lol, ", "honestly same here, ", "so real about languages: ", "biggest cheat code is "),
        ("reading messy daily slang on feeds sticks way harder than textbooks", "casual tiny replies build intuition ten times faster than flashcard drills", "picking up how real people joke beats memorizing stiff grammar charts", "five minutes chatting on timelines teaches phrases apps never mention"),
        (", keep chatting daily!", " — progress sneaks up fast", ", dropping a like for fluency", ", way more fun that way too"),
    ),
    ("ja", "tech_discussion"): (
        ("コード書くと本当、", "夜中のデバッグって、", "開発あるあるですが、", "息抜きがてら拝見、", "個人開発やってると、", "タイムライン見てて、"),
        ("普通に動く道筋はすぐできても例外の穴埋めが地味に時間溶けますよね", "ローカルで完璧に見えても実環境で一晩回すと急に別の顔出しますよね", "急がずゆっくり間隔あけて回す方が結局あとで一番楽なんですよね", "複雑に凝るより後からすぐ追える素直な作りにしておくと助かりますよね", "画面側のちょっとした変化に振り回されるの分かりみ深すぎます", "いったんパソコン閉じて飲み物とりに立つと意外にあっさり気づけますよね"),
        ("…お疲れさまです！無理せず休みつつ", "笑 今日もマイペースでいきましょ〜", "、応援してますね！休憩挟んでください", "、ひとまずいいね置いていきます〜"),
    ),
    ("ja", "vent_rant"): (
        ("ほんとお疲れさまです、", "いや分かりますそれ、", "夜遅くまで大変ですね、", "ふらっと見かけて一言、", "無理しすぎないで、", "いったん深呼吸して、"),
        ("煮詰まった画面をずっと睨むより温かい飲み物飲んで離れるのが一番ですね", "不思議なバグほど一晩ぐっすり寝た朝にあっさり見えたりしますよね", "今日はもう十分頑張ったと思うので自分を責めすぎず休んでください", "ペース乱されそうな日こそ早めに切り上げて体力温存するのが正解です"),
        ("、今夜はゆっくり寝てくださいね！", "〜明日いい方向に向かいますように", "、陰ながら応援してますね", "！無理なくマイペースでいきましょ"),
    ),
    ("ja", "social_chatter"): (
        ("タイムラインから失礼、", "ちょっと休憩中に発見、", "ふらっとお邪魔します、", "ゆるい空気いいですね、", "通りすがりにひとこと、", "お茶飲みつつ拝見して、"),
        ("画面見続けて固まった頭がふわっと軽くなる投稿で和みました", "こうして普通の日常をつぶやいてる方がタイムライン楽しくて好きです", "忙しい合間でも少し力を抜いて雑談できる雰囲気いいですよね〜", "たまには肩の力を抜いてぼーっとTL眺める時間大事にしたいです"),
        ("！今日もマイペースにいきましょ〜", "、またふらっと遊びにきますね笑", "、素敵な一日になりますように！", "、ひとまずいいねで挨拶していきます〜"),
    ),
    ("ja", "milestone"): (
        ("達成おめでとうございます！", "わあすごい節目ですね、", "こっそりお祝いです、", "素敵な積み重ねですね、", "拝見して嬉しくなり、", "マイペースな発信で、"),
        ("コツコツ自分のペースで書き続けてここまで届くの本当にかっこいいです", "楽しい雰囲気に自然な繋がりがどんどん集まっていくの見ているだけで元気出ます", "数字以上にこうして気軽に言葉交わせる仲間が増えていくの嬉しいですよね"),
        ("、これからもゆるく応援してます！", "〜次の目標も楽しみにしてますね", "！無理せず楽しく続けてください"),
    ),
    ("ja", "creative"): (
        ("素敵な世界観ですね、", "創作おつかれさまです、", "見ててほっとします、", "丁寧な表現すてきです、", "タイムラインで出会えて、"),
        ("流行に振り回されず自分のリズムで作品育ててる姿勢がとても心地いいです", "パソコン作業の合間にこういう温かい投稿に触れると気持ちがふわっと軽くなります", "ゆっくり自分のペースで積み重なっていく作品ってやっぱり強い魅力がありますね"),
        ("、休憩も挟みつつ頑張ってください〜", "！次の更新もゆるく楽しみにしてます", "、ささやかながらいいね置いていきます"),
    ),
    ("ko", "tech_discussion"): (
        ("진짜 공감합니다, ", "개발하다 보면 항상 ", "잠깐 쉬면서 보는데 ", "개인 프로젝트할 때 ", "밤늦게 디버깅할 땐 ", "슬쩍 인사 남기면 "),
        ("행복 경로는 금방 붙는데 예외랑 타이밍 잡다가 하루가 순식간에 지나가죠", "로컬에선 말짱하던 게 실제 환경에 올려서 밤새 돌리면 꼭 엉뚱한 데가 튀죠", "급하게 들이받기보다 천천히 간격 두고 돌리는 게 결국 훨씬 안전하더라고요", "일단 물 한 잔 마시고 잠깐 바람 쐬고 오면 안 보이던 게 금방 잡히곤 해요"),
        ("ㅎㅎ 오늘도 고생 많으십니다!", "ㅋㅋ 쉬엄쉬엄 챙기면서 하세요~", " 파이팅입니다, 좋아요 누르고 가요!", " 잠깐 눈 좀 쉬어가세요!"),
    ),
    ("ko", "social_chatter"): (
        ("타임라인 보다가 반가워 ", "잠깐 커피 마시면서 ", "지나가다 슬쩍 들러 ", "일상 글 너무 좋네요 ", "편한 분위기 반가워서 "),
        ("종일 까만 화면만 보다가 이렇게 사람 냄새 나는 글 보면 기분이 확 풀리네요", "바쁜 와중에도 잠시 숨 고르며 가볍게 수다 떠는 시간이 참 소중한 듯해요", "가끔은 할 일 목록 잊고 타임라인 천천히 구경하는 여유도 필요하죠"),
        ("ㅎㅎ 오늘도 기분 좋은 하루 보내세요!", "~ 자주 놀러 올게요!", " 좋아요 꾹 누르고 갑니다~"),
    ),
}


def _iter_varied_natural_candidates(
    ctx: dict[str, Any],
    rng: random.Random,
    *,
    max_yield: int = 500,
) -> list[str]:
    """Generate hundreds of non-AI, grammatically fluent contextual combinations via diagonal pairing."""
    import math

    lang = str(ctx.get("lang") or "en")
    intent = str(ctx.get("intent") or "unknown")
    if intent in {"too_short", "unknown"}:
        return []
    key = (lang, intent)
    if key not in _VARIED_REPLY_POOLS:
        fallback_intent = "social_chatter" if intent in {"creative", "language_learning", "milestone", "notif_follow"} else intent
        key = (lang, fallback_intent) if (lang, fallback_intent) in _VARIED_REPLY_POOLS else ("en", "social_chatter")
    raw_openers, raw_middles, raw_closers = _VARIED_REPLY_POOLS[key]

    openers = list(raw_openers)
    zh_h = str(ctx.get("zh_hook") or "").strip()
    en_h = str(ctx.get("en_hook") or "").strip()
    if intent == "tech_discussion":
        if lang == "zh" and zh_h:
            openers = [f"看你聊到{zh_h}，", f"最近也搞{zh_h}，"] + openers
        elif lang == "en" and en_h:
            openers = [f"using {en_h}, ", f"with {en_h}, "] + openers

    middles = list(raw_middles)
    closers = list(raw_closers)
    rng.shuffle(openers)
    rng.shuffle(middles)
    rng.shuffle(closers)

    n_o, n_m, n_c = len(openers), len(middles), len(closers)
    step_o = next((s for s in (7, 5, 3, 2, 1) if math.gcd(s, n_o) == 1), 1)
    out: list[str] = []
    # Sweep ALL (middle, closer) pairs across distinct coprime opener rotations first so neither
    # (opener, middle) nor (middle, closer) repeats until n_m * n_c (~432+) unique combos are drawn.
    for round_k in range(max(n_o, n_c)):
        c_offset = round_k % n_c
        o_shift = (round_k * step_o + (round_k // n_c)) % n_o
        for m_idx in range(n_m):
            c_idx = (m_idx + c_offset) % n_c
            o_idx = (m_idx + o_shift) % n_o
            candidate = f"{openers[o_idx]}{middles[m_idx]}{closers[c_idx]}".strip()
            if len(candidate) <= 280:
                out.append(candidate)
                if len(out) >= max_yield:
                    return out
    return out


def draft_friendly_reply(
    post: dict[str, Any],
    *,
    persona_memory: dict[str, Any] | None = None,
    recent_replies: list[str] | set[str] | _ReplyDeduper | None = None,
    rng: random.Random | None = None,
) -> str | None:
    """Draft a varied, casual-first, caring non-AI reply matched strictly to cleaned post intent."""
    r = rng or random.SystemRandom()
    raw_text = str(post.get("text") or post.get("text_preview") or "").strip()
    if not raw_text:
        return None
    ctx = _extract_post_context(post, persona_memory=persona_memory)
    if ctx["intent"] in {"too_short", "unknown"}:
        return None
    pool = _reply_candidates_for_context(ctx)
    if not pool:
        return None
    extra_banned = list((persona_memory or {}).get("banned_phrases") or [])
    anchors = [h.casefold() for h in (ctx.get("zh_hook"), ctx.get("en_hook")) if h]
    # Keep top life-empathy lines first when explicit life hooks are present; otherwise prioritize tech anchors.
    if ctx.get("life_hooks") and ctx["intent"] in {"vent_rant", "social_chatter"}:
        front = list(pool[:4])
        rest = list(pool[4:])
        r.shuffle(front)
        r.shuffle(rest)
        ordered_pool = front + rest
    elif anchors:
        anchored = [c for c in pool if any(a in c.casefold() for a in anchors)]
        others = [c for c in pool if c not in anchored]
        r.shuffle(anchored)
        r.shuffle(others)
        ordered_pool = anchored + others
    else:
        ordered_pool = list(pool)
        r.shuffle(ordered_pool)
    for candidate in ordered_pool:
        if (
            len(candidate) <= 280
            and not check_reply_naturalness(candidate, extra_banned=extra_banned)
            and not _is_duplicate_reply(candidate, recent_replies)
        ):
            return candidate
    for candidate in _iter_varied_natural_candidates(ctx, r):
        if (
            len(candidate) <= 280
            and not check_reply_naturalness(candidate, extra_banned=extra_banned)
            and not _is_duplicate_reply(candidate, recent_replies)
        ):
            return candidate
    return None
_STATUS_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})/status/(\d{1,25})(?:\?.*)?$",
    re.IGNORECASE,
)


class OrchestratorError(Exception):
    """Structured error safe to surface through MCP."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def load_safe_profile(path: Path | None = None) -> dict[str, Any]:
    """Load the safe pacing profile, falling back to built-in defaults if missing."""
    target = path or DEFAULT_SAFE_PROFILE_PATH
    raw: dict[str, Any] = {}
    if target.is_file():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except ValueError:
            raw = {}

    actions: dict[str, dict[str, Any]] = {}
    key_map = {
        "follow_profile": "follow",
        "repost_post": "repost",
        "like_post": "like",
        "reply_post": "reply",
        "publish_post": "publish",
    }
    for action_type, short_key in key_map.items():
        base = dict(DEFAULT_SAFE_CAPS[action_type])
        override = raw.get(short_key)
        if isinstance(override, dict):
            for field in ("enabled", "per_day", "min_gap_seconds", "max_gap_seconds", "per_wave"):
                if field in override and override[field] is not None:
                    base[field] = override[field]
        actions[action_type] = base

    cb = dict(DEFAULT_CIRCUIT_BREAKER)
    if isinstance(raw.get("circuit_breaker"), dict):
        cb.update(raw["circuit_breaker"])

    warmup = dict(DEFAULT_WARMUP)
    if isinstance(raw.get("warmup"), dict):
        warmup.update(raw["warmup"])

    return {
        "profile_version": raw.get("profile_version", 2),
        "actions": actions,
        "circuit_breaker": cb,
        "warmup": warmup,
    }


def _sorted_actions(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    actions = ledger.get("actions")
    if not isinstance(actions, dict):
        return []
    rows = [item for item in actions.values() if isinstance(item, dict)]
    rows.sort(
        key=lambda item: (
            _parse_epoch(item.get("completed_at"))
            or _parse_epoch(item.get("created_at"))
            or 0.0,
            str(item.get("action_id") or ""),
        )
    )
    return rows


def evaluate_circuit_breaker(
    ledger: dict[str, Any],
    *,
    profile: dict[str, Any] | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Inspect durable ledger refusals and uncertain streaks for write safety."""
    now = float(now_epoch) if now_epoch is not None else time.time()
    prof = profile or load_safe_profile()
    cb = prof["circuit_breaker"]
    refusal_cooldown_sec = float(cb.get("cooldown_hours_after_refusal", 24)) * 3600.0
    uncertain_cooldown_sec = float(cb.get("cooldown_hours_after_uncertain_streak", 0.5)) * 3600.0
    security_cooldown_sec = float(cb.get("cooldown_hours_after_security", 2)) * 3600.0
    uncertain_limit = int(cb.get("stop_on_uncertain_streak", 2))

    rows = _sorted_actions(ledger)
    latest_refusal: dict[str, Any] | None = None
    latest_security_at: float | None = None
    trailing_uncertain = 0
    latest_uncertain_at: float | None = None

    for action in rows:
        status = action.get("status")
        error = action.get("error") or {}
        details = error.get("details") or {}
        rejection = details.get("rejection")
        verdict = classify_refusal(rejection if isinstance(rejection, dict) else None)

        if verdict == "suspended":
            return {
                "status": "suspended",
                "allow_write": False,
                "reason": "ACCOUNT_SUSPENDED",
                "cooldown_until_epoch": None,
                "refusal": {
                    "action_id": action.get("action_id"),
                    "code": (rejection or {}).get("code"),
                    "message": str((rejection or {}).get("message") or "")[:160],
                    "at": action.get("completed_at"),
                },
                "uncertain_streak": trailing_uncertain,
            }

        if verdict in {"policy", "allowance"}:
            at_epoch = _parse_epoch(action.get("completed_at")) or _parse_epoch(action.get("created_at"))
            latest_refusal = {
                "action_id": action.get("action_id"),
                "verdict": verdict,
                "code": (rejection or {}).get("code"),
                "message": str((rejection or {}).get("message") or "")[:160],
                "at_epoch": at_epoch,
            }

        if error.get("code") == "X_SECURITY_INTERSTITIAL":
            sec_at = _parse_epoch(action.get("completed_at")) or _parse_epoch(action.get("created_at"))
            if sec_at is not None:
                latest_security_at = sec_at

        if status == "uncertain":
            trailing_uncertain += 1
            latest_uncertain_at = _parse_epoch(action.get("completed_at")) or _parse_epoch(action.get("created_at"))
        elif status == "completed":
            trailing_uncertain = 0
            latest_uncertain_at = None
            comp_at = _parse_epoch(action.get("completed_at"))
            if latest_security_at is not None and comp_at is not None and comp_at >= latest_security_at + 60.0:
                latest_security_at = None

    if latest_refusal and latest_refusal.get("at_epoch") is not None:
        until = float(latest_refusal["at_epoch"]) + refusal_cooldown_sec
        if now < until:
            return {
                "status": "cooldown",
                "allow_write": False,
                "reason": "REFUSAL_COOLDOWN",
                "cooldown_until_epoch": until,
                "refusal": latest_refusal,
                "uncertain_streak": trailing_uncertain,
            }

    if latest_security_at is not None:
        until = float(latest_security_at) + security_cooldown_sec
        if now < until:
            return {
                "status": "cooldown",
                "allow_write": False,
                "reason": "SECURITY_INTERSTITIAL_COOLDOWN",
                "cooldown_until_epoch": until,
                "refusal": None,
                "uncertain_streak": trailing_uncertain,
            }

    if trailing_uncertain >= uncertain_limit and latest_uncertain_at is not None:
        until = latest_uncertain_at + uncertain_cooldown_sec
        if now < until:
            return {
                "status": "cooldown",
                "allow_write": False,
                "reason": "UNCERTAIN_STREAK_COOLDOWN",
                "cooldown_until_epoch": until,
                "refusal": None,
                "uncertain_streak": trailing_uncertain,
            }

    # Respect X_RATE_LIMITED cooldown derived by daily_action_budget / follow_orchestration.
    if ledger.get("account_binding"):
        budget = evaluate_daily_action_budget(ledger, now_epoch=now)
        if budget.get("blocked"):
            detail = budget.get("blocked_detail") or {}
            if detail.get("reason") in {"cooldown_active", "manual_recovery_required"}:
                return {
                    "status": "cooldown",
                    "allow_write": False,
                    "reason": str(detail.get("reason") or "RATE_LIMIT_COOLDOWN").upper(),
                    "cooldown_until_epoch": detail.get("wait_until_epoch"),
                    "refusal": None,
                    "uncertain_streak": trailing_uncertain,
                }

    return {
        "status": "active",
        "allow_write": True,
        "reason": "healthy",
        "cooldown_until_epoch": None,
        "refusal": None,
        "uncertain_streak": trailing_uncertain,
    }


def _count_today_publishes(ledger: dict[str, Any], now_epoch: float) -> int:
    day = _local_date(now_epoch)
    count = 0
    for action in _sorted_actions(ledger):
        if action.get("type") != "publish_post" or action.get("status") != "completed":
            continue
        result = action.get("result") or {}
        if result.get("verified") is not True:
            continue
        at_epoch = _parse_epoch(action.get("completed_at"))
        if at_epoch is not None and _local_date(at_epoch) == day:
            count += 1
    return count


X_RATE_LIMIT_REFERENCE: dict[str, dict[str, Any]] = {
    "follow_profile": {
        "endpoint": "friendships/create (Web) / POST /2/users/:id/following (API v2)",
        "web_limit_per_900s": 15,
        "api_v2_limit_per_900s": 50,
        "platform_daily_max_standard": 400,
        "platform_daily_max_premium": 1000,
        "observed_risk_note": "Observed web 900s cap=15 (HTTP 429) plus separate cumulative allowance code 161 (HTTP 403) around ~215 follows on newer accounts.",
        "safe_per_15m": 10,
        "safe_per_1h": 24,
        "recommended_per_day": 40,
    },
    "publish_post": {
        "endpoint": "CreateTweet (Web) / POST /2/tweets (API v2)",
        "web_limit_per_day_unverified": 50,
        "api_v2_limit_per_900s": 100,
        "api_v2_app_per_24h": 10000,
        "observed_risk_note": "Rolling 30-min velocity friction triggers above bursts; space standalone original posts >= 30 min (threads chain sequentially).",
        "safe_per_15m": 2,
        "safe_per_1h": 3,
        "min_standalone_gap_seconds": 1800,
        "recommended_per_day": 5,
    },
    "reply_post": {
        "endpoint": "CreateTweet in_reply_to (Web) / POST /2/tweets (API v2)",
        "web_limit_per_day_unverified": 200,
        "web_limit_per_day_verified": 400,
        "api_v2_limit_per_900s": 100,
        "observed_risk_note": "Shares tweet creation velocity (~200/day unverified, up to ~400/day Verified) and duplicate-text spam detectors; space 55-108s in high-reply mode (75-155s default).",
        "safe_per_15m": 10,
        "safe_per_1h": 32,
        "recommended_per_day": 240,
    },
    "like_post": {
        "endpoint": "FavoriteTweet (Web) / POST /2/users/:id/likes (API v2)",
        "header_window_limit": 500,
        "soft_daily_max": 500,
        "observed_risk_note": "Scarecrow code 283 blocks rapid hourly bursts (> ~50-100/hr) or low-standing accounts long before header quota hits 0.",
        "safe_per_15m": 14,
        "safe_per_1h": 36,
        "recommended_per_day": 60,
    },
    "repost_post": {
        "endpoint": "CreateRetweet (Web)",
        "observed_risk_note": "Postmortem 2026-11-14: rapid automated reposts (7s median) escalated directly to code 141 account suspension even under HTTP 200.",
        "safe_per_15m": 1,
        "safe_per_1h": 2,
        "recommended_per_day": 4,
    },
}

_ROLLING_ACTION_TYPES = ("follow_profile", "publish_post", "reply_post", "like_post", "repost_post")


def evaluate_rolling_windows(
    ledger: dict[str, Any] | None,
    *,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Compute rolling 15-minute (900s) and 1-hour (3600s) X write velocity usage and remaining safe headroom."""
    now = float(now_epoch) if now_epoch is not None else time.time()
    cutoff_15m = now - 900.0
    cutoff_1h = now - 3600.0

    ts_by_type: dict[str, list[float]] = {k: [] for k in _ROLLING_ACTION_TYPES}
    actions = (ledger or {}).get("actions")
    if isinstance(actions, dict):
        for act in actions.values():
            if not isinstance(act, dict):
                continue
            atype = str(act.get("type") or "")
            if atype not in ts_by_type:
                continue
            status = str(act.get("status") or "")
            # Count completed, active claimed, and uncertain attempts (X saw the write), while excluding rejected local/skipped failures.
            if status not in {"completed", "claimed", "uncertain"}:
                continue
            ep = _parse_epoch(act.get("completed_at") or act.get("claimed_at") or act.get("created_at"))
            if ep is None or ep > now + 5.0:
                continue
            if ep >= cutoff_1h:
                ts_by_type[atype].append(ep)

    used_15m: dict[str, int] = {}
    used_1h: dict[str, int] = {}
    safe_15m: dict[str, int] = {}
    safe_1h: dict[str, int] = {}
    rem_15m: dict[str, int] = {}
    rem_1h: dict[str, int] = {}
    rolling_rem: dict[str, int] = {}
    next_reset: dict[str, float | None] = {}

    for k in _ROLLING_ACTION_TYPES:
        stamps = sorted(ts_by_type[k])
        stamps_15m = [t for t in stamps if t >= cutoff_15m]
        c15 = int(X_RATE_LIMIT_REFERENCE[k]["safe_per_15m"])
        c60 = int(X_RATE_LIMIT_REFERENCE[k]["safe_per_1h"])
        u15 = len(stamps_15m)
        u60 = len(stamps)
        r15 = max(0, c15 - u15)
        r60 = max(0, c60 - u60)

        used_15m[k] = u15
        used_1h[k] = u60
        safe_15m[k] = c15
        safe_1h[k] = c60
        rem_15m[k] = r15
        rem_1h[k] = r60
        rolling_rem[k] = min(r15, r60)

        resets: list[float] = []
        if r15 == 0 and stamps_15m:
            resets.append(stamps_15m[-c15] + 900.0)
        if r60 == 0 and stamps:
            resets.append(stamps[-c60] + 3600.0)
        next_reset[k] = round(max(resets), 2) if resets else None

    return {
        "now_epoch": now,
        "used_15m": used_15m,
        "used_1h": used_1h,
        "safe_15m_caps": safe_15m,
        "safe_1h_caps": safe_1h,
        "remaining_15m": rem_15m,
        "remaining_1h": rem_1h,
        "rolling_remaining": rolling_rem,
        "next_reset_epoch": next_reset,
        "rate_limit_reference": X_RATE_LIMIT_REFERENCE,
    }


def slice_wave_for_rate_limits(
    operations: list[dict[str, Any]],
    rolling_windows: dict[str, Any] | None,
    *,
    wave_limit: int = 12,
    max_reposts_per_wave: int = 1,
) -> dict[str, Any]:
    """Slice an immediate micro-wave respecting X 15m/1h rolling rate limits cleanly."""
    rem = dict((rolling_windows or {}).get("rolling_remaining") or {
        k: int(X_RATE_LIMIT_REFERENCE[k]["safe_per_15m"])
        for k in _ROLLING_ACTION_TYPES
    })
    next_resets = dict((rolling_windows or {}).get("next_reset_epoch") or {})
    selected: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    reposts_used = 0
    standalone_posts_used = 0

    for op in operations:
        if len(selected) >= wave_limit:
            break
        action = str(op.get("action") or "")
        if action == "repost_post" and reposts_used >= max_reposts_per_wave:
            skipped[action] = skipped.get(action, 0) + 1
            continue
        # Only admit <= 1 standalone original post per immediate micro-wave so posts stay >= 30m apart.
        if action == "publish_post" and not op.get("thread_id") and standalone_posts_used >= 1:
            skipped[action] = skipped.get(action, 0) + 1
            continue
        if action in rem and int(rem[action]) <= 0:
            skipped[action] = skipped.get(action, 0) + 1
            continue
        if action in rem:
            rem[action] = int(rem[action]) - 1
        if action == "repost_post":
            reposts_used += 1
        if action == "publish_post" and not op.get("thread_id"):
            standalone_posts_used += 1
        selected.append(op)

    blocked_resets = [
        float(next_resets[k])
        for k in skipped
        if next_resets.get(k) is not None
    ]
    return {
        "operations": selected,
        "skipped_by_rate_limit": skipped,
        "next_retry_epoch": min(blocked_resets) if blocked_resets else None,
    }


def evaluate_account_health(
    ledger: dict[str, Any],
    *,
    account_snapshot: dict[str, Any] | None = None,
    content_queue: list[dict[str, Any]] | None = None,
    profile: dict[str, Any] | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Compute standing score (0-100), warmup readiness, safe daily budget, and next steps."""
    now = float(now_epoch) if now_epoch is not None else time.time()
    prof = profile or load_safe_profile()
    breaker = evaluate_circuit_breaker(ledger, profile=prof, now_epoch=now)
    budget = evaluate_daily_action_budget(ledger, now_epoch=now)

    achieved_today: dict[str, int] = dict(budget["achieved"])
    achieved_today["publish_post"] = _count_today_publishes(ledger, now)

    safe_caps: dict[str, int] = {}
    remaining_safe: dict[str, int] = {}
    for action_type, cfg in prof["actions"].items():
        cap = int(cfg.get("per_day", 0)) if cfg.get("enabled", True) else 0
        safe_caps[action_type] = cap
        remaining_safe[action_type] = max(0, cap - achieved_today.get(action_type, 0))

    # Profile & warmup readiness.
    snap = account_snapshot if isinstance(account_snapshot, dict) else {}
    has_snapshot = bool(snap)
    missing_fields: list[str] = []
    if has_snapshot:
        if not str(snap.get("display_name") or snap.get("name") or "").strip():
            missing_fields.append("name")
        if not str(snap.get("bio") or "").strip():
            missing_fields.append("bio")
        if snap.get("has_avatar") is False:
            missing_fields.append("avatar")
        profile_score = max(0, 20 - len(missing_fields) * 7)
    else:
        profile_score = 10  # Neutral when live snapshot wasn't supplied.

    # Original content score (0-30).
    warmup_cfg = prof["warmup"]
    req_posts = int(warmup_cfg.get("required_original_posts", 10))
    req_days = int(warmup_cfg.get("required_days_before_automation", 7))
    ledger_publishes = sum(
        1 for a in _sorted_actions(ledger)
        if a.get("type") == "publish_post" and a.get("status") == "completed"
    )
    posts_count = int(snap.get("posts_count", ledger_publishes)) if snap.get("posts_count") is not None else ledger_publishes
    active_days = {
        _local_date(ep)
        for a in _sorted_actions(ledger)
        for ep in (_parse_epoch(a.get("completed_at")),)
        if ep is not None
    }
    account_age_days = int(snap.get("account_age_days", len(active_days))) if snap.get("account_age_days") is not None else len(active_days)
    original_ratio = min(1.0, posts_count / max(1, req_posts))
    age_ratio = min(1.0, account_age_days / max(1, req_days))
    original_score = round(20 * original_ratio + 10 * age_ratio)

    # Engagement balance score (0-25).
    total_writes_today = sum(achieved_today.values())
    over_cap = any(achieved_today.get(k, 0) > safe_caps.get(k, 0) for k in safe_caps)
    if over_cap:
        engagement_score = 10
    elif total_writes_today > 0:
        engagement_score = min(25, 15 + min(10, total_writes_today // 3))
    else:
        engagement_score = 15  # Clean baseline ready for daily activity.

    # Safety cleanliness score (0-25).
    if breaker["status"] == "suspended":
        safety_score = 0
    elif breaker["status"] == "cooldown":
        safety_score = 5
    else:
        safety_score = max(0, 25 - breaker["uncertain_streak"] * 8)

    standing_score = max(0, min(100, profile_score + original_score + engagement_score + safety_score))
    warmup_ready = (posts_count >= req_posts) and (account_age_days >= req_days) and not missing_fields
    established_on_x = posts_count >= 50

    if account_age_days < 3 and posts_count < 3:
        warmup_stage = "ghost"
        stage_caps = {"publish_post": 0, "like_post": 3, "follow_profile": 0, "repost_post": 0, "reply_post": 0}
    elif account_age_days < 7 and posts_count < 10:
        warmup_stage = "light_engage"
        stage_caps = {"publish_post": 1, "like_post": 12, "follow_profile": 2, "repost_post": 1, "reply_post": 5}
    elif not warmup_ready and not established_on_x:
        warmup_stage = "content_build"
        stage_caps = {"publish_post": 2, "like_post": 25, "follow_profile": 5, "repost_post": 3, "reply_post": 12}
    else:
        warmup_stage = "growth"
        stage_caps = {"publish_post": 5, "like_post": 60, "follow_profile": 200, "repost_post": 8, "reply_post": 25}

    followers_snap = snap.get("followers_count")
    following_snap = snap.get("following_count")
    ratio_guard = (
        isinstance(following_snap, (int, float))
        and isinstance(followers_snap, (int, float))
        and following_snap >= 50
        and following_snap > max(1.0, float(followers_snap)) * 2.5
    )
    if ratio_guard:
        stage_caps["follow_profile"] = 0

    queued_items = [item for item in (content_queue or []) if item.get("status") == "queued"]
    due_items = [
        item for item in queued_items
        if item.get("scheduled_after_epoch") is None or float(item["scheduled_after_epoch"]) <= now
    ]

    recommended_actions: list[dict[str, Any]] = []
    if not breaker["allow_write"]:
        recommended_actions.append({
            "action": "wait_cooldown",
            "priority": "high",
            "reason": breaker["reason"],
            "cooldown_until_epoch": breaker["cooldown_until_epoch"],
        })
    else:
        if missing_fields:
            recommended_actions.append({
                "action": "complete_profile",
                "priority": "high",
                "missing_fields": missing_fields,
            })
        if due_items and remaining_safe["publish_post"] > 0:
            recommended_actions.append({
                "action": "publish_post",
                "priority": "high",
                "suggested_count": min(len(due_items), remaining_safe["publish_post"], 2),
                "min_gap_seconds": prof["actions"]["publish_post"]["min_gap_seconds"],
            })
        elif posts_count < req_posts and remaining_safe["publish_post"] > 0:
            recommended_actions.append({
                "action": "queue_original_post",
                "priority": "high",
                "reason": f"Build original post standing ({posts_count}/{req_posts}).",
            })
        if remaining_safe["like_post"] > 0:
            recommended_actions.append({
                "action": "like_post",
                "priority": "medium",
                "suggested_count": min(10, remaining_safe["like_post"]),
                "min_gap_seconds": prof["actions"]["like_post"]["min_gap_seconds"],
            })
        if warmup_stage in {"light_engage", "content_build"} and remaining_safe.get("reply_post", 0) > 0 and stage_caps["reply_post"] > 0:
            recommended_actions.append({
                "action": "drop_by_visit_and_reply",
                "priority": "high",
                "suggested_count": min(5, remaining_safe["reply_post"], stage_caps["reply_post"]),
                "reason": "Early-stage growth tactic: visit peer profiles, like 1-2 recent posts, and leave 1 casual caring reply.",
            })
        if ratio_guard:
            recommended_actions.append({
                "action": "improve_follower_ratio",
                "priority": "high",
                "reason": f"Following ({int(following_snap)}) > 2.5x followers ({int(followers_snap)}); pause follows and focus on original posts.",
            })
        if remaining_safe["follow_profile"] > 0 and stage_caps["follow_profile"] > 0:
            recommended_actions.append({
                "action": "follow_profile",
                "priority": "medium",
                "suggested_count": min(5, remaining_safe["follow_profile"], stage_caps["follow_profile"]),
                "min_gap_seconds": prof["actions"]["follow_profile"]["min_gap_seconds"],
            })
        if remaining_safe["repost_post"] > 0:
            recommended_actions.append({
                "action": "repost_post",
                "priority": "low",
                "suggested_count": min(3, remaining_safe["repost_post"]),
                "min_gap_seconds": prof["actions"]["repost_post"]["min_gap_seconds"],
            })

    rolling = evaluate_rolling_windows(ledger, now_epoch=now)
    exhausted_windows = {
        k: rolling["next_reset_epoch"][k]
        for k in _ROLLING_ACTION_TYPES
        if rolling["rolling_remaining"].get(k, 1) == 0 and rolling["next_reset_epoch"].get(k) is not None
    }
    if breaker["allow_write"] and exhausted_windows:
        for k, reset_ep in sorted(exhausted_windows.items()):
            wait_sec = max(1, int(round(float(reset_ep) - now)))
            recommended_actions.append({
                "action": "wait_rolling_window",
                "target_action": k,
                "priority": "high",
                "wait_seconds": wait_sec,
                "next_reset_epoch": reset_ep,
                "reason": f"Rolling 15m/1h velocity cap reached for {k}; wait {wait_sec}s to stay below X rate limits.",
            })

    return {
        "account_status": breaker["status"],
        "allow_write": breaker["allow_write"],
        "circuit_breaker": breaker,
        "standing_score": standing_score,
        "score_breakdown": {
            "profile_readiness": profile_score,
            "original_content": original_score,
            "engagement_balance": engagement_score,
            "safety_cleanliness": safety_score,
            "snapshot_provided": has_snapshot,
        },
        "warmup": {
            "ready": warmup_ready,
            "stage": warmup_stage,
            "stage_caps": stage_caps,
            "ratio_guard_triggered": ratio_guard,
            "posts_count": posts_count,
            "required_original_posts": req_posts,
            "active_days": account_age_days,
            "required_days": req_days,
            "missing_profile_fields": missing_fields,
        },
        "today": {
            "local_day": budget["local_day"],
            "achieved": achieved_today,
            "safe_caps": safe_caps,
            "remaining_safe": remaining_safe,
        },
        "rolling_windows": rolling,
        "content_queue": {
            "queued": len(queued_items),
            "due": len(due_items),
        },
        "recommended_actions": recommended_actions,
    }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def default_tech_casual_persona_template() -> dict[str, Any]:
    """Foundational human casual-first persona preset for a developer on X."""
    return {
        "role": "平时爱在 X 上轻松闲聊吐槽、碰到真技术帖才顺手聊两句的写代码选手",
        "voice_rules": [
            "日常主调是轻松闲聊+带点程序员冷幽默的吐槽感，说话像真人朋友聊天，不端着、不科普、不说官话",
            "先提取原帖正文（去掉 @昵称、Replying to 和末尾点赞数）；像「咋了」这类超短或缺乏上下文的句子绝不硬回，宁可只点赞",
            "遇到加班/改bug破防、出摊交友、粉丝破百/破千、学语言或小说日常，按正常人类口吻接茬吐槽或祝贺，禁止强扯系统架构",
            "只有对方明确在聊代码/AI/系统工程时，才带出真实研发视角，但依然用口语化短句（禁双破折号总结和AI腔）",
            "严禁 AI 套话（禁感谢分享/很有深度/同路人常联系/留个脚印/Solid breakdown/delicate balance/实战踩坑）与短期重复句式",
        ],
        "content_pillars": {
            "日常闲聊与开发吐槽": 40,
            "Agent工程与MCP": 25,
            "BuildInPublic日常": 20,
            "AI开源与系统可靠性": 15,
        },
        "banned_phrases": [
            "game-changer",
            "synergy",
            "Solid note",
            "Solid breakdown",
            "delicate balance",
            "Respect the pragmatic",
            "beat flashy demos",
            "Hard-won insight",
            "teach twice as much",
            "赋能",
            "抓手",
            "闭环思维",
            "非常有深度",
            "很有深度",
            "感谢分享",
            "感谢互动交流",
            "说得太好了",
            "受益匪浅",
            "收到你的补充",
            "实战踩坑",
            "底层工程闭环",
            "后验确认",
            "同路人常联系",
            "留个脚印关注",
            "稳步往上走",
            "Caught this on my feed",
            "Dropping by to say hi",
            "互粉",
        ],
    }


class PersonaMemoryStore:
    """Durable store for owner persona rules, content pillars, and episodic memory."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "persona_memory.json"
        self.lock = LedgerLock(self.state_dir / ".persona_memory.lock")

    @staticmethod
    def _default() -> dict[str, Any]:
        return {
            "version": 1,
            "role": "",
            "voice_rules": [],
            "content_pillars": {},
            "inferred_active_hours": None,
            "banned_phrases": ["game-changer", "synergy", "很有深度", "感谢分享", "说得太好了"],
            "episodic_notes": [],
            "feedback_notes": [],
            "owner_sync": {
                "last_sync_at": None,
                "last_sync_epoch": 0.0,
                "owner_username": "",
                "owner_display_name": "",
                "owner_bio": "",
                "observed_topics": [],
                "correction_log": [],
            },
        }

    def _read_unlocked(self) -> dict[str, Any]:
        base = self._default()
        if not self.path.exists():
            return base
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                base.update(loaded)
        except ValueError:
            pass
        return base

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def read(self) -> dict[str, Any]:
        with self.lock:
            return self._read_unlocked()

    def update(
        self,
        *,
        role: str | None = None,
        voice_rules: list[str] | None = None,
        content_pillars: dict[str, int] | None = None,
        banned_phrases: list[str] | None = None,
        inferred_active_hours: list[int] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            data = self._read_unlocked()
            if role is not None:
                data["role"] = str(role).strip()[:160]
            if isinstance(voice_rules, list):
                data["voice_rules"] = [str(r).strip()[:200] for r in voice_rules if str(r).strip()][:20]
            if isinstance(content_pillars, dict):
                data["content_pillars"] = {
                    str(k).strip()[:40]: max(0, min(100, int(v)))
                    for k, v in content_pillars.items()
                    if str(k).strip()
                }
            if isinstance(banned_phrases, list):
                data["banned_phrases"] = [str(p).strip()[:80] for p in banned_phrases if str(p).strip()][:50]
            if isinstance(inferred_active_hours, (list, tuple)) and len(inferred_active_hours) == 2:
                s_h, e_h = int(inferred_active_hours[0]), int(inferred_active_hours[1])
                if 0 <= s_h < e_h <= 24:
                    data["inferred_active_hours"] = [s_h, e_h]
            data["updated_at"] = _iso(time.time())
            self._write_unlocked(data)
            return data

    def add_episode(
        self,
        *,
        topic: str,
        angle: str,
        post_url: str | None = None,
        outcome: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "at": _iso(time.time()),
            "topic": str(topic or "").strip()[:80],
            "angle": str(angle or "").strip()[:200],
            "post_url": str(post_url).strip() if post_url else None,
            "outcome": str(outcome).strip()[:80] if outcome else None,
        }
        with self.lock:
            data = self._read_unlocked()
            notes = list(data.get("episodic_notes") or [])
            notes.append(entry)
            data["episodic_notes"] = notes[-30:]
            self._write_unlocked(data)
            return data

    def add_feedback(self, note: str) -> dict[str, Any]:
        cleaned = str(note or "").strip()[:240]
        if not cleaned:
            raise OrchestratorError("INVALID_ARGUMENT", "feedback note must be non-empty.")
        with self.lock:
            data = self._read_unlocked()
            notes = list(data.get("feedback_notes") or [])
            notes.append({"at": _iso(time.time()), "note": cleaned})
            data["feedback_notes"] = notes[-20:]
            sync = dict(data.get("owner_sync") or self._default()["owner_sync"])
            corr = list(sync.get("correction_log") or [])
            corr.append({"at": _iso(time.time()), "kind": "owner_feedback", "summary": cleaned[:160]})
            sync["correction_log"] = corr[-15:]
            data["owner_sync"] = sync
            self._write_unlocked(data)
            return data

    def reconcile_owner_persona(
        self,
        *,
        owner_snapshot: dict[str, Any] | None = None,
        own_posts: list[dict[str, Any]] | None = None,
        ledger: dict[str, Any] | None = None,
        username: str | None = None,
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        """Timely sync from owner's X profile/posts without erasing manual owner background rules."""
        now = float(now_epoch) if now_epoch is not None else time.time()
        snap = owner_snapshot if isinstance(owner_snapshot, dict) else {}
        resolved_user = str(username or snap.get("username") or "").strip().lstrip("@")
        display_name = str(snap.get("display_name") or "").strip()[:80]
        bio = str(snap.get("bio") or "").strip()[:240]
        posts_input = list(own_posts or [])
        synthetic_posts = list(posts_input)
        if bio:
            synthetic_posts.append({"text": bio})
        inferred = infer_owner_profile(synthetic_posts, ledger=ledger, username=resolved_user)

        with self.lock:
            data = self._read_unlocked()
            sync = dict(self._default()["owner_sync"])
            if isinstance(data.get("owner_sync"), dict):
                sync.update(data["owner_sync"])
            changes: list[str] = []

            if display_name and display_name != sync.get("owner_display_name"):
                changes.append(f"主人昵称更新为: {display_name}")
                sync["owner_display_name"] = display_name
            if bio and bio != sync.get("owner_bio"):
                changes.append(f"主人简介更新: {bio[:80]}")
                sync["owner_bio"] = bio
            if resolved_user:
                sync["owner_username"] = resolved_user

            new_topics = [str(t).strip() for t in (inferred.get("persona_topics") or []) if str(t).strip()][:8]
            prev_topics = list(sync.get("observed_topics") or [])
            if new_topics and new_topics != prev_topics:
                added = [t for t in new_topics if t not in prev_topics]
                sync["observed_topics"] = new_topics
                if added:
                    changes.append(f"识别主人近期新话题: {', '.join(added[:4])}")
                pillars = dict(data.get("content_pillars") or {})
                for t in added[:3]:
                    key = t[:40]
                    if key and key not in pillars and len(pillars) < 8:
                        pillars[key] = 15
                if pillars:
                    data["content_pillars"] = pillars

            # Fill missing baseline fields if persona has never been initialized yet.
            if not str(data.get("role") or "").strip() and inferred.get("inferred_role"):
                data["role"] = str(inferred["inferred_role"])[:160]
                changes.append(f"自动补全角色定位: {data['role']}")
            if not (data.get("voice_rules") or []) and inferred.get("inferred_voice_rules"):
                data["voice_rules"] = list(inferred["inferred_voice_rules"])

            if len(posts_input) >= 3 and inferred.get("active_hours"):
                hours = inferred["active_hours"]
                if data.get("inferred_active_hours") != hours:
                    data["inferred_active_hours"] = hours
                    changes.append(f"根据主人近期发帖修正活跃时段: {hours[0]}:00-{hours[1]}:00")

            sync["last_sync_at"] = _iso(now)
            sync["last_sync_epoch"] = now
            sync["sample_posts"] = len(posts_input)
            sync["style_notes"] = inferred.get("style_notes") or {}
            if changes:
                corr = list(sync.get("correction_log") or [])
                for c in changes:
                    corr.append({"at": _iso(now), "kind": "auto_sync", "summary": c[:160]})
                sync["correction_log"] = corr[-15:]
            data["owner_sync"] = sync
            data["updated_at"] = _iso(now)
            self._write_unlocked(data)
            return {
                "synced": True,
                "changes": changes,
                "persona": data,
            }


class ContentQueueStore:
    """Durable JSON store for scheduled/queued personal X posts and replies."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "content_queue.json"
        self.lock = LedgerLock(self.state_dir / ".content_queue.lock")

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "items": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data
        except ValueError:
            pass
        return {"version": 1, "items": []}

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(
        self,
        *,
        kind: str,
        text: str = "",
        topic: str | None = None,
        target_url: str | None = None,
        scheduled_after_epoch: float | None = None,
        extra_banned: list[str] | None = None,
        tweets: list[str] | None = None,
    ) -> dict[str, Any]:
        if kind not in {"original_post", "reply", "thread"}:
            raise OrchestratorError("INVALID_ARGUMENT", "kind must be original_post, reply, or thread.")
        cleaned_tweets: list[str] = []
        if kind == "thread":
            if not isinstance(tweets, list) or not 2 <= len(tweets) <= 10:
                raise OrchestratorError("INVALID_ARGUMENT", "thread items require 2 to 10 tweets.")
            for idx, part in enumerate(tweets):
                t = str(part or "").strip()
                if not t or len(t) > 280:
                    raise OrchestratorError("INVALID_ARGUMENT", f"thread tweet #{idx + 1} must be 1 to 280 characters.")
                if _SECRET_PATTERN.search(t):
                    raise OrchestratorError("SECRET_DETECTED", "Refusing to store text matching a secret pattern.")
                lowered_part = t.casefold()
                for phrase in extra_banned or []:
                    p = str(phrase or "").strip().casefold()
                    if p and p in lowered_part:
                        raise OrchestratorError("BANNED_PHRASE", f"Refusing to store text matching banned phrase: '{phrase}'.")
                cleaned_tweets.append(t)
            cleaned = cleaned_tweets[0]
        else:
            cleaned = str(text or "").strip()
            if not cleaned or len(cleaned) > 280:
                raise OrchestratorError("INVALID_ARGUMENT", "text must be 1 to 280 characters.")
            if _SECRET_PATTERN.search(cleaned):
                raise OrchestratorError("SECRET_DETECTED", "Refusing to store text matching a secret pattern.")
            lowered = cleaned.casefold()
            for phrase in extra_banned or []:
                p = str(phrase or "").strip().casefold()
                if p and p in lowered:
                    raise OrchestratorError("BANNED_PHRASE", f"Refusing to store text matching banned phrase: '{phrase}'.")
            if kind == "reply":
                if not target_url or not _STATUS_URL_RE.match(str(target_url).strip()):
                    raise OrchestratorError("INVALID_ARGUMENT", "reply items require a valid X status target_url.")
                cliches = check_reply_naturalness(cleaned, extra_banned=extra_banned)
                if cliches:
                    raise OrchestratorError("REPLY_TOO_GENERIC", "; ".join(cliches))
        item = {
            "item_id": "cq_" + secrets.token_hex(6),
            "kind": kind,
            "text": cleaned,
            "topic": str(topic).strip()[:80] if topic else None,
            "target_url": str(target_url).strip() if target_url else None,
            "scheduled_after_epoch": float(scheduled_after_epoch) if scheduled_after_epoch is not None else None,
            "status": "queued",
            "created_at": _iso(time.time()),
            "published_url": None,
            "action_id": None,
        }
        if kind == "thread":
            item["tweets"] = cleaned_tweets
        with self.lock:
            data = self._read()
            norm = cleaned.casefold()
            for existing in data["items"]:
                if existing.get("status") in {"queued", "published"} and str(existing.get("text") or "").strip().casefold() == norm:
                    raise OrchestratorError("DUPLICATE_CONTENT", "Matching text already exists in the content queue.")
            data["items"].append(item)
            self._write(data)
        return item

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            items = list(self._read()["items"])
        if status:
            return [item for item in items if item.get("status") == status]
        return items

    def due(self, *, now_epoch: float | None = None) -> list[dict[str, Any]]:
        now = float(now_epoch) if now_epoch is not None else time.time()
        queued = self.list(status="queued")
        return [
            item for item in queued
            if item.get("scheduled_after_epoch") is None or float(item["scheduled_after_epoch"]) <= now
        ]

    def remove(self, item_id: str) -> dict[str, Any]:
        with self.lock:
            data = self._read()
            for item in data["items"]:
                if item.get("item_id") == item_id:
                    item["status"] = "cancelled"
                    self._write(data)
                    return item
        raise OrchestratorError("QUEUE_ITEM_NOT_FOUND", f"Content queue item {item_id} not found.")

    def mark_published(
        self,
        item_id: str,
        *,
        published_url: str | None = None,
        action_id: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            data = self._read()
            for item in data["items"]:
                if item.get("item_id") == item_id:
                    item["status"] = "published"
                    item["published_at"] = _iso(time.time())
                    if published_url:
                        item["published_url"] = published_url
                    if action_id:
                        item["action_id"] = action_id
                    self._write(data)
                    return item
        raise OrchestratorError("QUEUE_ITEM_NOT_FOUND", f"Content queue item {item_id} not found.")


class TargetPoolStore:
    """Durable pre-collected target pool (from scout accounts or offline scripts) so main account avoids search bursts."""

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / "target_pool.json"
        self.lock = LedgerLock(Path(state_dir) / ".target_pool.lock")

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "candidates": [], "posts": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "version": 1,
                    "candidates": list(data.get("candidates") or []),
                    "posts": list(data.get("posts") or []),
                }
        except ValueError:
            pass
        return {"version": 1, "candidates": [], "posts": []}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def stats(self) -> dict[str, Any]:
        with self.lock:
            data = self._read()
        fresh_c = sum(1 for c in data["candidates"] if c.get("status") == "fresh")
        fresh_p = sum(1 for p in data["posts"] if p.get("status") == "fresh")
        return {
            "fresh_candidates": fresh_c,
            "fresh_posts": fresh_p,
            "total_candidates": len(data["candidates"]),
            "total_posts": len(data["posts"]),
            "path": str(self.path),
        }

    def list(self, *, status: str = "fresh") -> dict[str, Any]:
        with self.lock:
            data = self._read()
        return {
            "candidates": [c for c in data["candidates"] if not status or c.get("status") == status],
            "posts": [p for p in data["posts"] if not status or p.get("status") == status],
        }

    def clear(self) -> dict[str, Any]:
        with self.lock:
            self._write({"version": 1, "candidates": [], "posts": []})
        return self.stats()

    def ingest(
        self,
        *,
        candidates: list[dict[str, Any]] | None = None,
        posts: list[dict[str, Any]] | None = None,
        source: str = "scout_precollect",
    ) -> dict[str, Any]:
        added_c = 0
        added_p = 0
        now_str = _iso(time.time())
        with self.lock:
            data = self._read()
            existing_handles = {str(c.get("handle") or "").strip().lstrip("@").casefold() for c in data["candidates"]}
            existing_urls = {str(p.get("url") or "").strip().casefold() for p in data["posts"]}
            for raw in candidates or []:
                if not isinstance(raw, dict):
                    continue
                handle = str(raw.get("handle") or raw.get("username") or "").strip().lstrip("@")
                if not _HANDLE_RE.match(handle) or handle.casefold() in existing_handles:
                    continue
                existing_handles.add(handle.casefold())
                data["candidates"].append({
                    "handle": handle,
                    "bio": str(raw.get("bio") or "")[:240],
                    "post_text": str(raw.get("post_text") or "")[:240],
                    "followers_count": raw.get("followers_count"),
                    "following_count": raw.get("following_count"),
                    "active": bool(raw.get("active", True)),
                    "mutual_verified": bool(raw.get("mutual_verified", False)),
                    "source": str(raw.get("source") or source)[:60],
                    "status": "fresh",
                    "added_at": now_str,
                })
                added_c += 1
            for raw_p in posts or []:
                if not isinstance(raw_p, dict):
                    continue
                m = _STATUS_URL_RE.match(str(raw_p.get("url") or raw_p.get("post_url") or "").strip())
                if not m:
                    continue
                canonical = f"https://x.com/{m.group(1).casefold()}/status/{m.group(2)}"
                if canonical.casefold() in existing_urls:
                    continue
                existing_urls.add(canonical.casefold())
                data["posts"].append({
                    "url": canonical,
                    "author": str(raw_p.get("author") or m.group(1)).lstrip("@"),
                    "text": str(raw_p.get("text") or "")[:280],
                    "source": str(raw_p.get("source") or source)[:60],
                    "status": "fresh",
                    "added_at": now_str,
                })
                added_p += 1
            data["candidates"] = data["candidates"][-1000:]
            data["posts"] = data["posts"][-1000:]
            self._write(data)
        return {"added_candidates": added_c, "added_posts": added_p, **self.stats()}

    def pop_batch(
        self,
        *,
        max_candidates: int = 10,
        max_posts: int = 15,
        exclude_handles: set[str] | None = None,
        exclude_urls: set[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        ex_h = {h.casefold() for h in (exclude_handles or set())}
        ex_u = {u.casefold() for u in (exclude_urls or set())}
        picked_c: list[dict[str, Any]] = []
        picked_p: list[dict[str, Any]] = []
        with self.lock:
            data = self._read()
            changed = False
            for c in data["candidates"]:
                if c.get("status") != "fresh":
                    continue
                h = str(c.get("handle") or "").casefold()
                if h in ex_h:
                    c["status"] = "consumed"
                    changed = True
                    continue
                if len(picked_c) < max_candidates:
                    c["status"] = "consumed"
                    changed = True
                    picked_c.append(dict(c))
            for p in data["posts"]:
                if p.get("status") != "fresh":
                    continue
                u = str(p.get("url") or "").casefold()
                if u in ex_u:
                    p["status"] = "consumed"
                    changed = True
                    continue
                if len(picked_p) < max_posts:
                    p["status"] = "consumed"
                    changed = True
                    picked_p.append(dict(p))
            if changed:
                self._write(data)
        return {"candidates": picked_c, "posts": picked_p}


def _seen_ledger_targets(ledger: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return (seen_handles, seen_status_urls) from completed or active ledger actions."""
    handles: set[str] = set()
    urls: set[str] = set()
    for action in _sorted_actions(ledger):
        if action.get("status") not in {"completed", "pending", "claimed"}:
            continue
        payload = action.get("payload") or {}
        if action.get("type") in {"follow_profile", "unfollow_profile"}:
            handle = str(payload.get("handle") or "").strip().lstrip("@").casefold()
            if handle:
                handles.add(handle)
        elif action.get("type") in {"like_post", "repost_post", "reply_post"}:
            url = str(payload.get("post_url") or payload.get("url") or "").strip()
            match = _STATUS_URL_RE.match(url)
            if match:
                urls.add(f"https://x.com/{match.group(1).casefold()}/status/{match.group(2)}")
    return handles, urls


def _unfollowed_blocked_handles(ledger: dict[str, Any]) -> set[str]:
    """Return handles that were unfollowed for non-reciprocity so future waves ignore/block them."""
    blocked: set[str] = set()
    mutual_map = ledger.get("mutual_followers") if isinstance(ledger.get("mutual_followers"), dict) else {}
    known_mutuals = {str(k).strip().lstrip("@").casefold() for k in mutual_map.keys() if str(k).strip()}
    for action in _sorted_actions(ledger):
        if action.get("status") != "completed":
            continue
        atype = action.get("type")
        payload = action.get("payload") or {}
        result = action.get("result") or {}
        handle = str(result.get("handle") or payload.get("handle") or "").strip().lstrip("@").casefold()
        if not handle:
            continue
        if atype == "unfollow_profile" and result.get("state") == "unfollowed":
            if handle not in known_mutuals:
                blocked.add(handle)
        elif atype == "follow_profile" and result.get("mutual_verified") is True:
            blocked.discard(handle)
    return blocked


def _recent_author_engagements(
    ledger: dict[str, Any],
    *,
    window_hours: float = 18.0,
    now_epoch: float | None = None,
) -> set[str]:
    """Return author handles engaged recently (like/repost/reply) so waves avoid over-touching one person."""
    now = float(now_epoch) if now_epoch is not None else time.time()
    cutoff = now - max(1.0, float(window_hours)) * 3600.0
    authors: set[str] = set()
    for action in _sorted_actions(ledger):
        if action.get("status") not in {"completed", "pending", "claimed"}:
            continue
        if action.get("type") not in {"like_post", "repost_post", "reply_post"}:
            continue
        ep = _parse_epoch(action.get("completed_at")) or _parse_epoch(action.get("created_at"))
        if ep is not None and ep < cutoff:
            continue
        payload = action.get("payload") or {}
        result = action.get("result") or {}
        author = str(result.get("author") or payload.get("author") or "").strip().lstrip("@").casefold()
        if not author:
            m = _STATUS_URL_RE.match(str(payload.get("post_url") or payload.get("url") or "").strip())
            if m:
                author = m.group(1).casefold()
        if author:
            authors.add(author)
    return authors


_RECIPROCAL_MARKERS = (
    "互关", "互follow", "互粉", "回关",
    "follow back", "followback", "f4f", "mutual", "let's connect",
    "build in public", "indie hacker",
    "相互フォロー", "フォロバ", "相互",
    "맞팔", "소통",
    "sigueme", "te sigo",
)
RECIPROCAL_SEARCH_QUERY = "互关 OR 互follow OR 互粉"
CROSS_LANGUAGE_DISCOVERY_QUERIES = (
    "build in public OR indie hacker OR AI agent",
    "AI Agent OR MCP OR LLM OR devlog",
    "follow back OR followback OR mutuals AI",
    "相互フォロー OR フォロバ100 OR 個人開発",
    "개발자 OR 인디해커 OR AI 에이전트",
    "indie dev OR open source OR SaaS builder",
    "互关 OR 互follow OR AI 独立开发",
)
_ZOMBIE_SPAM_MARKERS = (
    "airdrop", "giveaway", "抽奖", "free mint", "crypto signal",
    "dm for promo", "casino", "onlyfans", "18+", "bot account",
)


def pick_discovery_query(window: dict[str, Any] | None, wave_index: int = 0) -> str:
    """Rotate across owner's inferred topics and cross-language discovery queries."""
    w = window if isinstance(window, dict) else {}
    custom = w.get("discovery_queries")
    pool = [str(q).strip() for q in custom if str(q).strip()] if isinstance(custom, list) else []
    base = str(w.get("search_query") or "").strip()
    topics = [str(t).strip() for t in (w.get("persona_topics") or []) if str(t).strip()]
    if not pool:
        pool = []
        if base:
            pool.append(base)
        for i in range(0, len(topics), 2):
            chunk = " OR ".join(topics[i:i + 2])
            if chunk and chunk not in pool:
                pool.append(chunk)
        for q in CROSS_LANGUAGE_DISCOVERY_QUERIES:
            if q not in pool:
                pool.append(q)
    return pool[abs(int(wave_index)) % len(pool)]


def has_reciprocal_marker(cand: dict[str, Any]) -> bool:
    """Return True when candidate handle/name/bio/post advertises reciprocal follow."""
    combined = " ".join([
        str(cand.get("handle") or cand.get("username") or ""),
        str(cand.get("display_name") or ""),
        str(cand.get("bio") or ""),
        str(cand.get("post_text") or ""),
    ]).casefold()
    return any(marker in combined for marker in _RECIPROCAL_MARKERS)


CONCRETE_STRATEGIES: tuple[str, ...] = (
    "reciprocal_first",
    "niche_sniper_bundle",
    "thread_reply_magnet",
    "reply_then_follow",
    "balanced",
)
ALLOWED_STRATEGIES: frozenset[str] = frozenset({"auto_fastest", *CONCRETE_STRATEGIES})

STRATEGY_SPECS: dict[str, dict[str, Any]] = {
    "auto_fastest": {
        "id": "auto_fastest",
        "label": "智能择优增粉",
        "short_goal": "自动归因择优",
        "stickiness_tier": "adaptive",
        "summary": "基于近7日真实粉丝增量、回关率与留存粘性自动切换当前表现最快策略（冷启动阶段自动轮换采样）。",
    },
    "reciprocal_first": {
        "id": "reciprocal_first",
        "label": "互关快速起号",
        "short_goal": "前期快速起量",
        "stickiness_tier": "volume_fast",
        "summary": "优先回关通知与明确互关候选，高频点赞+短句互动配合 4-5 人微批关注，最快拉高前期关注基数。",
    },
    "niche_sniper_bundle": {
        "id": "niche_sniper_bundle",
        "label": "精准同好三连",
        "short_goal": "同圈层高回关",
        "stickiness_tier": "high_sticky",
        "summary": "筛选活跃中小号技术同行，必须完成『串门+点赞+走心回复』串联捆绑再关注，兼顾速度与高回关粘性。",
    },
    "thread_reply_magnet": {
        "id": "thread_reply_magnet",
        "label": "干货内容+抢评引流",
        "short_goal": "自然被动涨粘性粉",
        "stickiness_tier": "high_sticky",
        "summary": "优先发布原创/串推并重仓高质量长评论与精选技术转发，严格收紧冷关注，靠主页回访沉淀高粘性自然粉。",
    },
    "reply_then_follow": {
        "id": "reply_then_follow",
        "label": "高粘性深聊留存",
        "short_goal": "零盲关注高比值",
        "stickiness_tier": "high_sticky",
        "summary": "完全暂停冷关注，专注通知互动、串门点赞与真人感深聊，仅回关主动关注或高频互动对象。",
    },
    "balanced": {
        "id": "balanced",
        "label": "稳健均衡配比",
        "short_goal": "全维度平滑增长",
        "stickiness_tier": "balanced",
        "summary": "均衡混合原创发帖、通知回访、适度关注与高频点赞，风控余量充足，适合日常长期稳定维护。",
    },
}


def list_orchestration_strategies() -> list[dict[str, Any]]:
    """Return canonical metadata for all supported orchestration strategies."""
    return [dict(STRATEGY_SPECS[key]) for key in (
        "auto_fastest",
        "reciprocal_first",
        "niche_sniper_bundle",
        "thread_reply_magnet",
        "reply_then_follow",
        "balanced",
    )]


def score_follow_candidate(cand: dict[str, Any], *, strategy: str = "reciprocal_first") -> tuple[int, str]:
    """Score a follow candidate for real human/builder quality and reciprocal growth (higher is better)."""
    score = 0
    bio = str(cand.get("bio") or "").strip()
    post_text = str(cand.get("post_text") or "").strip()
    source = str(cand.get("source") or "").strip().lower()
    combined = f"{bio} {post_text}".casefold()

    if any(spam in combined for spam in _ZOMBIE_SPAM_MARKERS):
        score -= 60
    if source in {"notification_follow", "notification_followback"}:
        score += 55
    elif source.startswith("notification_"):
        score += 35
    if cand.get("mutual_verified") is True:
        score += 40
    if has_reciprocal_marker(cand):
        score += 55 if strategy == "reciprocal_first" else 40
    if bio:
        score += 15
    if cand.get("visited_profile") is True:
        score += 25 if strategy == "niche_sniper_bundle" else 15
    if cand.get("recent_post_at") or cand.get("active") is True:
        score += 25
    elif not bio and not post_text:
        score -= 30 if strategy == "niche_sniper_bundle" else 20

    followers = cand.get("followers_count")
    following = cand.get("following_count")
    if isinstance(followers, (int, float)) and isinstance(following, (int, float)) and following > 0:
        ratio = followers / following
        if followers < 5 and following > 500:
            score -= 40
        elif 0.2 <= ratio <= 3.0:
            score += 25
        elif followers > 10000 and ratio > 10.0:
            score -= 35 if strategy == "niche_sniper_bundle" else 25
    if strategy == "niche_sniper_bundle" and isinstance(followers, (int, float)):
        if 15 <= followers <= 8000:
            score += 25
        elif followers > 20000:
            score -= 25
    handle = str(cand.get("handle") or cand.get("username") or "").strip().lstrip("@").casefold()
    return (score, handle)


_STOPWORDS = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "have", "are", "was", "were",
    "you", "your", "just", "will", "not", "but", "can", "all", "one", "out", "about",
    "https", "http", "com", "status", "nov", "oct", "dec", "jan", "feb", "mar", "apr",
    "may", "jun", "jul", "aug", "sep", "的", "了", "在", "是", "我", "有", "和", "就",
    "不", "人", "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你",
    "会", "着", "没有", "看", "好", "自己", "这", "怎么", "以后", "最近", "什么",
})


def find_unfollow_candidates(
    ledger: dict[str, Any],
    *,
    min_age_days: int = 3,
    now_epoch: float | None = None,
) -> list[dict[str, Any]]:
    """Return oldest-first follows older than min_age_days with no mutual_verified or follower proof."""
    now = float(now_epoch) if now_epoch is not None else time.time()
    min_age_sec = max(1, int(min_age_days)) * 86400.0
    unfollowed: set[str] = set()
    follows: dict[str, dict[str, Any]] = {}
    mutual_map = ledger.get("mutual_followers") if isinstance(ledger.get("mutual_followers"), dict) else {}
    known_mutuals = {str(k).strip().lstrip("@").casefold() for k in mutual_map.keys() if str(k).strip()}

    for action in _sorted_actions(ledger):
        if action.get("status") != "completed":
            continue
        atype = action.get("type")
        payload = action.get("payload") or {}
        result = action.get("result") or {}
        handle = str(result.get("handle") or payload.get("handle") or "").strip().lstrip("@")
        if not handle:
            continue
        key = handle.casefold()
        if atype == "unfollow_profile":
            if result.get("state") == "unfollowed":
                unfollowed.add(key)
            elif result.get("mutual_verified") is True:
                known_mutuals.add(key)
                follows.pop(key, None)
        elif atype == "follow_profile" and result.get("verified") is True and result.get("state") == "following":
            unfollowed.discard(key)
            if result.get("mutual_verified") is True or key in known_mutuals:
                follows.pop(key, None)
                continue
            ep = _parse_epoch(action.get("completed_at"))
            if ep is not None:
                follows[key] = {
                    "handle": handle,
                    "profile_url": f"https://x.com/{handle}",
                    "followed_at": action.get("completed_at"),
                    "followed_epoch": ep,
                    "age_days": round((now - ep) / 86400.0, 2),
                    "action_id": action.get("action_id"),
                }

    due = [
        row for key, row in follows.items()
        if key not in unfollowed and key not in known_mutuals and (now - row["followed_epoch"]) >= min_age_sec
    ]
    due.sort(key=lambda row: row["followed_epoch"])
    return due


def infer_owner_profile(
    own_posts: list[dict[str, Any]],
    *,
    ledger: dict[str, Any] | None = None,
    username: str | None = None,
) -> dict[str, Any]:
    """Infer owner topics, search query, active hours, and writing style from own posts."""
    counts: dict[str, int] = {}
    lengths: list[int] = []
    zh_chars = 0
    en_chars = 0
    handle_cf = (username or "").strip().lstrip("@").casefold()

    for post in own_posts or []:
        raw = str(post.get("text") or "").strip()
        if not raw:
            continue
        lengths.append(len(raw))
        for ch in raw:
            if "\u4e00" <= ch <= "\u9fff":
                zh_chars += 1
            elif ch.isascii() and ch.isalpha():
                en_chars += 1
        # Extract ASCII tech/domain words (>=2 chars) and 2-4 char Chinese terms.
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,20}", raw):
            low = tok.casefold()
            if low in _STOPWORDS or low.isdigit() or (handle_cf and (low in handle_cf or handle_cf in low)):
                continue
            counts[tok] = counts.get(tok, 0) + 1
        for tok in re.findall(r"[\u4e00-\u9fff]{2,4}", raw):
            if tok in _STOPWORDS:
                continue
            counts[tok] = counts.get(tok, 0) + 1

    ranked = sorted(counts.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)
    topics = [k for k, _ in ranked[:8]]
    ascii_topics = [k for k in topics if k.isascii()]
    if len(ascii_topics) >= 2:
        search_query = " ".join(ascii_topics[:2])
    elif topics:
        search_query = " ".join(topics[:2])
    else:
        search_query = ""

    local_tz = datetime.now().astimezone().tzinfo or LOCAL_TIMEZONE
    hours: list[int] = []
    for post in own_posts or []:
        ts = post.get("created_at") or post.get("posted_at") or post.get("timestamp")
        ep = _parse_epoch(ts) if isinstance(ts, str) else None
        if ep is not None:
            hours.append(datetime.fromtimestamp(ep, local_tz).hour)
    if isinstance(ledger, dict):
        for action in (ledger.get("actions") or {}).values():
            if not isinstance(action, dict) or action.get("status") != "completed":
                continue
            ep = _parse_epoch(action.get("completed_at"))
            if ep is not None:
                hours.append(datetime.fromtimestamp(ep, local_tz).hour)
    if hours:
        start_h = max(0, min(hours) - 1)
        end_h = min(24, max(hours) + 2)
        if end_h <= start_h:
            start_h, end_h = 9, 23
    else:
        start_h, end_h = 9, 23

    avg_len = round(sum(lengths) / len(lengths)) if lengths else 0
    lang = "zh+en" if (zh_chars > 10 and en_chars > 10) else ("zh" if zh_chars >= en_chars else "en")
    inferred_role = " / ".join(topics[:3]) if topics else ""
    inferred_voice_rules = [
        f"language: {lang}",
        f"typical_length: ~{avg_len} chars" if avg_len else "concise, natural tone",
    ]
    return {
        "persona_topics": topics,
        "exclude_keywords": ["airdrop", "giveaway", "抽奖", "广告"],
        "search_query": search_query,
        "active_hours": [start_h, end_h],
        "inferred_role": inferred_role,
        "inferred_voice_rules": inferred_voice_rules,
        "style_notes": {
            "sample_posts": len(lengths),
            "avg_length": avg_len,
            "language": lang,
        },
    }


def score_post_relevance(
    post: dict[str, Any],
    *,
    persona_topics: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
) -> tuple[bool, int]:
    """Return (allowed, relevance_score) for a timeline/search/drop-by post."""
    text = str(post.get("text") or "").casefold()
    excludes = [str(k).strip().casefold() for k in (exclude_keywords or []) if str(k).strip()]
    if any(k in text for k in excludes):
        return (False, -100)
    topics = [str(k).strip().casefold() for k in (persona_topics or []) if str(k).strip()]
    score = sum(10 for k in topics if k in text)
    source = str(post.get("source") or "").strip().lower()
    if source == "notification":
        notif_kind = str(post.get("notif_kind") or "").strip().lower()
        score += 30 if notif_kind in {"follow", "reply", "mention"} else 20
    if post.get("visited_profile"):
        score += 18
    if post.get("author_bio"):
        score += 5
    # Low-crowd drop-by bonus: replies under quiet threads (<=5 replies) from peer-size accounts get seen first.
    replies = post.get("reply_count")
    if isinstance(replies, (int, float)):
        if 0 <= replies <= 5:
            score += 10
        elif replies > 80:
            score -= 10
    author_followers = post.get("author_followers")
    if isinstance(author_followers, (int, float)):
        if 20 <= author_followers <= 5000:
            score += 8
        elif author_followers > 50000:
            score -= 8
    return (True, score)


_ENGAGEMENT_NUM_RE = re.compile(r"(\d+)\s*(?:likes?|replies?|reply|follows?|follow-backs?|reposts?|bookmarks?|赞|回复|回关|转发)", re.IGNORECASE)


def compute_pillar_weights(
    ledger: dict[str, Any],
    content_queue: list[dict[str, Any]] | None = None,
    persona_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute engagement-weighted priority scores per content pillar from episodes, queue, and ledger."""
    stats: dict[str, dict[str, Any]] = {}

    def _ensure(topic: str) -> dict[str, Any]:
        key = topic.strip().casefold()
        if key not in stats:
            stats[key] = {
                "pillar": topic.strip(),
                "published_count": 0,
                "episode_count": 0,
                "engagement_signals": 0,
                "weight": 1.0,
            }
        return stats[key]

    for pillar in (persona_memory or {}).get("content_pillars") or []:
        if isinstance(pillar, str) and pillar.strip():
            _ensure(pillar)

    for item in content_queue or []:
        if not isinstance(item, dict):
            continue
        topic = str(item.get("topic") or "").strip()
        if not topic:
            continue
        entry = _ensure(topic)
        if item.get("status") == "published":
            entry["published_count"] += 1
            entry["engagement_signals"] += 1

    for ep in (persona_memory or {}).get("episodic_notes") or []:
        if not isinstance(ep, dict):
            continue
        topic = str(ep.get("topic") or "").strip()
        if not topic:
            continue
        entry = _ensure(topic)
        entry["episode_count"] += 1
        outcome = str(ep.get("outcome") or "")
        nums = [int(m.group(1)) for m in _ENGAGEMENT_NUM_RE.finditer(outcome)]
        entry["engagement_signals"] += sum(nums) if nums else (1 if outcome.strip() else 0)

    # Also credit verified actions whose payload carries a matching topic.
    for action in (ledger.get("actions") or {}).values():
        if not isinstance(action, dict) or action.get("status") != "completed":
            continue
        res = action.get("result") if isinstance(action.get("result"), dict) else {}
        if res.get("verified") is not True:
            continue
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        topic = str(payload.get("topic") or "").strip()
        if topic:
            entry = _ensure(topic)
            entry["engagement_signals"] += 2 if res.get("mutual_verified") else 1

    pillars_out: list[dict[str, Any]] = []
    for entry in stats.values():
        # Smooth weighting: base 1.0 + bounded logarithmic/linear signal boost.
        signals = entry["engagement_signals"]
        pub = entry["published_count"]
        ep_cnt = entry["episode_count"]
        weight = round(1.0 + min(3.0, signals * 0.15 + ep_cnt * 0.1 + pub * 0.05), 3)
        entry["weight"] = weight
        pillars_out.append(entry)

    pillars_out.sort(key=lambda r: (r["weight"], r["engagement_signals"], r["pillar"].casefold()), reverse=True)
    weights_map = {r["pillar"].casefold(): r["weight"] for r in pillars_out}
    return {
        "pillars": pillars_out,
        "weights_map": weights_map,
        "recommended_top": [r["pillar"] for r in pillars_out[:3]],
        "total_signals": sum(r["engagement_signals"] for r in pillars_out),
    }


def _bundle_author_interactions(
    buckets: dict[str, list[dict[str, Any]]],
    *,
    interaction_strategy: str,
) -> list[dict[str, Any]]:
    """Pair same-author like + contextual reply (+ reciprocal follow) into human-paced bundles."""
    likes_by_author: dict[str, list[dict[str, Any]]] = {}
    replies_by_author: dict[str, list[dict[str, Any]]] = {}
    follows_by_handle: dict[str, dict[str, Any]] = {}

    for item in buckets["like_post"]:
        key = str(item.get("author") or "").strip().lstrip("@").casefold()
        if key:
            likes_by_author.setdefault(key, []).append(item)
    for item in buckets["reply_post"]:
        key = str(item.get("author") or "").strip().lstrip("@").casefold()
        if key:
            replies_by_author.setdefault(key, []).append(item)
    for item in buckets["follow_profile"]:
        key = str(item.get("handle") or "").strip().lstrip("@").casefold()
        if key and key not in follows_by_handle:
            follows_by_handle[key] = item

    used_likes: set[int] = set()
    used_replies: set[int] = set()
    used_follows: set[int] = set()
    bundled_groups: list[tuple[int, list[dict[str, Any]]]] = []

    candidate_authors = sorted(
        set(replies_by_author.keys()),
        key=lambda a: (
            0 if any(r.get("source") == "notification" for r in replies_by_author[a]) else 1,
            0 if any(r.get("visited_before_engage") for r in replies_by_author[a]) else 1,
            a,
        ),
    )

    for author_key in candidate_authors:
        rep_items = [r for r in replies_by_author[author_key] if id(r) not in used_replies]
        if not rep_items:
            continue
        rep = rep_items[0]
        seq: list[dict[str, Any]] = []
        is_notif = rep.get("source") == "notification"
        notif_kind = str(rep.get("notif_kind") or "").lower()
        fol = follows_by_handle.get(author_key)
        author_like_pool = [l for l in likes_by_author.get(author_key, []) if id(l) not in used_likes]

        role = "inbound_followback" if (is_notif and notif_kind == "follow") else (
            "inbound_reply" if is_notif else "outbound_bundle"
        )
        if is_notif and fol is not None and id(fol) not in used_follows:
            fol["interaction_role"] = role
            fol["visited_before_engage"] = True
            seq.append(fol)
            used_follows.add(id(fol))
        # Early-stage drop-by ("串门"): warm up with 1-2 likes on recent posts before leaving 1 caring reply.
        for like in author_like_pool[:2]:
            like["interaction_role"] = role
            like["visited_before_engage"] = bool(like.get("visited_before_engage") or is_notif)
            seq.append(like)
            used_likes.add(id(like))
        rep["interaction_role"] = role
        seq.append(rep)
        used_replies.add(id(rep))
        if not is_notif and interaction_strategy != "reply_then_follow" and fol is not None and id(fol) not in used_follows:
            fol["interaction_role"] = role
            fol["visited_before_engage"] = bool(fol.get("visited_before_engage") or rep.get("visited_before_engage"))
            seq.append(fol)
            used_follows.add(id(fol))

        priority = 0 if is_notif else (1 if rep.get("visited_before_engage") else 2)
        bundled_groups.append((priority, seq))

    buckets["like_post"] = [x for x in buckets["like_post"] if id(x) not in used_likes]
    buckets["reply_post"] = [x for x in buckets["reply_post"] if id(x) not in used_replies]
    buckets["follow_profile"] = [x for x in buckets["follow_profile"] if id(x) not in used_follows]
    bundled_groups.sort(key=lambda pair: pair[0])
    return [op for _, seq in bundled_groups for op in seq]


def build_daily_orchestration_plan(
    ledger: dict[str, Any],
    *,
    goals: dict[str, int] | None = None,
    timeline_posts: list[dict[str, Any]] | None = None,
    follow_candidates: list[dict[str, Any]] | None = None,
    content_queue: list[dict[str, Any]] | None = None,
    account_snapshot: dict[str, Any] | None = None,
    persona_topics: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    require_reciprocal_marker: bool = False,
    pillar_weights: dict[str, float] | None = None,
    auto_reply: bool = False,
    interaction_strategy: str = "reciprocal_first",
    require_quality_repost: bool = True,
    persona_memory: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    autopilot_status: dict[str, Any] | None = None,
    wave_index: int = 0,
    now_epoch: float | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Build a deduplicated, safe-paced, persona-aware interaction & follow orchestration plan."""
    strategy_requested = str(interaction_strategy or "reciprocal_first").strip()
    if strategy_requested not in ALLOWED_STRATEGIES:
        raise OrchestratorError(
            "INVALID_ARGUMENT",
            f"interaction_strategy must be one of {', '.join(sorted(ALLOWED_STRATEGIES))}.",
        )
    strategy = strategy_requested
    strategy_reason = f"explicit:{strategy_requested}"
    if strategy_requested == "auto_fastest":
        from autopilot_ops import select_best_growth_strategy

        decision = select_best_growth_strategy(
            ledger,
            autopilot_status=autopilot_status or {},
            wave_index=wave_index,
            now_epoch=now_epoch,
        )
        strategy = str(decision["recommended_strategy"])
        strategy_reason = str(decision["reason"])
    now = float(now_epoch) if now_epoch is not None else time.time()
    prof = profile or load_safe_profile()
    # Allow explicit growth-stage follow goal up to 200/day, high-signal likes up to 250/day,
    # and proactive contextual replies up to 400/day while keeping reposts rare and curated.
    follow_goal = int(goals.get("follow_profile", 0)) if goals else 0
    reply_goal = int(goals.get("reply_post", 0)) if goals else 0
    like_goal = int(goals.get("like_post", 0)) if goals else 0
    updated_actions = dict(prof["actions"])
    prof_changed = False
    if follow_goal > int(prof["actions"]["follow_profile"]["per_day"]):
        follow_cfg = {
            **prof["actions"]["follow_profile"],
            "per_day": min(200, follow_goal),
        }
        updated_actions["follow_profile"] = follow_cfg
        if follow_goal >= 100:
            # Smoother human cadence (44-78s) paired with rolling 10/900s + 24/1h gates avoids X's 15/900s follow cliff.
            follow_cfg["min_gap_seconds"] = min(int(follow_cfg.get("min_gap_seconds", 90)), 44)
            follow_cfg["max_gap_seconds"] = min(int(follow_cfg.get("max_gap_seconds", 240)), 78)
            updated_actions["like_post"] = {
                **prof["actions"]["like_post"],
                "min_gap_seconds": min(int(prof["actions"]["like_post"].get("min_gap_seconds", 60)), 22),
                "max_gap_seconds": min(int(prof["actions"]["like_post"].get("max_gap_seconds", 180)), 52),
            }
            updated_actions["reply_post"] = {
                **prof["actions"]["reply_post"],
                "min_gap_seconds": max(75, int(prof["actions"]["reply_post"].get("min_gap_seconds", 75))),
                "max_gap_seconds": max(135, min(160, int(prof["actions"]["reply_post"].get("max_gap_seconds", 145)))),
            }
            updated_actions["repost_post"] = {
                **prof["actions"]["repost_post"],
                "min_gap_seconds": max(150, int(prof["actions"]["repost_post"].get("min_gap_seconds", 150))),
                "max_gap_seconds": max(240, int(prof["actions"]["repost_post"].get("max_gap_seconds", 360))),
            }
        prof_changed = True
    if like_goal > int(updated_actions["like_post"].get("per_day", 60)):
        updated_actions["like_post"] = {
            **updated_actions["like_post"],
            "per_day": min(250, like_goal),
            "min_gap_seconds": min(int(updated_actions["like_post"].get("min_gap_seconds", 60)), 18),
            "max_gap_seconds": min(int(updated_actions["like_post"].get("max_gap_seconds", 180)), 48),
        }
        prof_changed = True
    if reply_goal > int(updated_actions["reply_post"].get("per_day", 25)):
        high_reply = reply_goal >= 150
        updated_actions["reply_post"] = {
            **updated_actions["reply_post"],
            "per_day": min(400, reply_goal),
            "min_gap_seconds": 55 if high_reply else max(75, min(90, int(updated_actions["reply_post"].get("min_gap_seconds", 75)))),
            "max_gap_seconds": 108 if high_reply else max(135, min(155, int(updated_actions["reply_post"].get("max_gap_seconds", 145)))),
        }
        prof_changed = True
    if prof_changed:
        prof = {**prof, "actions": updated_actions}
    health = evaluate_account_health(
        ledger,
        account_snapshot=account_snapshot,
        content_queue=content_queue,
        profile=prof,
        now_epoch=now,
    )

    if not health["allow_write"]:
        return {
            "status": "blocked",
            "blocked": True,
            "reason": health["circuit_breaker"]["reason"],
            "cooldown_until_epoch": health["circuit_breaker"]["cooldown_until_epoch"],
            "health": health,
            "operation_count": 0,
            "operations": [],
            "estimated_duration_seconds": 0,
        }

    requested = goals or {
        "publish_post": 1,
        "like_post": 150,
        "follow_profile": 5,
        "repost_post": 2,
        "reply_post": 240,
    }
    stage = str(health["warmup"].get("stage") or "growth")
    stage_caps = dict(health["warmup"].get("stage_caps") or {})
    if stage == "growth":
        if like_goal > int(stage_caps.get("like_post", 60)):
            stage_caps["like_post"] = min(250, like_goal)
        if reply_goal > int(stage_caps.get("reply_post", 25)):
            stage_caps["reply_post"] = min(400, reply_goal)
    remaining = health["today"]["remaining_safe"]
    capped = {
        kind: max(0, min(int(requested.get(kind, 0)), int(remaining.get(kind, 0)), int(stage_caps.get(kind, 0))))
        for kind in ("publish_post", "like_post", "follow_profile", "repost_post", "reply_post")
    }

    seen_handles, seen_urls = _seen_ledger_targets(ledger)
    blocked_handles = _unfollowed_blocked_handles(ledger)
    recent_authors = _recent_author_engagements(ledger, window_hours=18.0, now_epoch=now)
    rng = random.Random(seed) if seed is not None else random.SystemRandom()

    buckets: dict[str, list[dict[str, Any]]] = {
        "publish_post": [],
        "like_post": [],
        "follow_profile": [],
        "repost_post": [],
        "reply_post": [],
    }

    # 1. Due original posts, threads, and replies from content queue (pillar-weighted).
    norm_weights = {str(k).strip().casefold(): float(v) for k, v in (pillar_weights or {}).items()}
    due_items: list[tuple[float, int, dict[str, Any]]] = []
    for idx, item in enumerate(content_queue or []):
        if item.get("status") != "queued":
            continue
        sched = item.get("scheduled_after_epoch")
        if sched is not None and float(sched) > now:
            continue
        topic_key = str(item.get("topic") or "").strip().casefold()
        w = norm_weights.get(topic_key, 1.0)
        due_items.append((-w, idx, item))
    due_items.sort(key=lambda t: (t[0], t[1]))

    thread_sequences: list[list[dict[str, Any]]] = []
    for _, _, item in due_items:
        kind = item.get("kind")
        if kind == "thread" and len(buckets["publish_post"]) < capped["publish_post"] and not thread_sequences:
            tweets = [str(t).strip() for t in (item.get("tweets") or []) if str(t).strip()]
            if len(tweets) >= 2:
                if stage != "growth" and any(_EXTERNAL_URL_RE.search(t) for t in tweets):
                    continue
                thread_id = str(item.get("item_id") or "")
                seq: list[dict[str, Any]] = [{
                    "action": "publish_post",
                    "text": tweets[0],
                    "queue_item_id": thread_id,
                    "topic": item.get("topic"),
                    "thread_id": thread_id,
                    "thread_index": 0,
                    "thread_total": len(tweets),
                }]
                for t_idx, t_text in enumerate(tweets[1:], start=1):
                    if len(buckets["reply_post"]) + t_idx > capped["reply_post"]:
                        break
                    seq.append({
                        "action": "reply_post",
                        "url": "",
                        "text": t_text,
                        "queue_item_id": item.get("item_id"),
                        "topic": item.get("topic"),
                        "thread_id": thread_id,
                        "thread_index": t_idx,
                        "thread_total": len(tweets),
                        "reply_to_previous_thread_step": True,
                    })
                if len(seq) >= 2:
                    thread_sequences.append(seq)
                    capped["publish_post"] = max(0, capped["publish_post"] - 1)
                    capped["reply_post"] = max(0, capped["reply_post"] - (len(seq) - 1))
        elif kind == "original_post" and len(buckets["publish_post"]) < capped["publish_post"]:
            if stage != "growth" and _EXTERNAL_URL_RE.search(str(item.get("text") or "")):
                continue
            buckets["publish_post"].append({
                "action": "publish_post",
                "text": item["text"],
                "queue_item_id": item.get("item_id"),
                "topic": item.get("topic"),
            })
        elif kind == "reply" and len(buckets["reply_post"]) < capped["reply_post"] and item.get("target_url"):
            match = _STATUS_URL_RE.match(str(item["target_url"]).strip())
            if match:
                canonical = f"https://x.com/{match.group(1).casefold()}/status/{match.group(2)}"
                if canonical not in seen_urls:
                    seen_urls.add(canonical)
                    buckets["reply_post"].append({
                        "action": "reply_post",
                        "url": canonical,
                        "author": match.group(1),
                        "text": item["text"],
                        "queue_item_id": item.get("item_id"),
                    })

    # 2. Deduplicated & topic-ranked timeline/search/visited/notification posts for engagement.
    clean_posts: list[dict[str, Any]] = []
    local_seen_urls = set(seen_urls)
    for post in (timeline_posts or []):
        if not isinstance(post, dict):
            continue
        allowed, rel_score = score_post_relevance(
            post,
            persona_topics=persona_topics,
            exclude_keywords=exclude_keywords,
        )
        if not allowed:
            continue
        raw_url = str(post.get("url") or post.get("post_url") or "").strip()
        match = _STATUS_URL_RE.match(raw_url)
        if not match:
            continue
        canonical = f"https://x.com/{match.group(1).casefold()}/status/{match.group(2)}"
        if canonical in local_seen_urls:
            continue
        local_seen_urls.add(canonical)
        author = str(post.get("author") or match.group(1)).lstrip("@")
        if author.casefold() in blocked_handles:
            continue
        fresh_bonus = 0 if (author.casefold() in recent_authors and str(post.get("source") or "") != "notification") else 8
        drop_by_bonus = 6 if (bool(post.get("visited_profile")) and stage in {"ghost", "light_engage", "content_build"}) else 0
        clean_posts.append({
            "url": canonical,
            "author": author,
            "author_bio": str(post.get("author_bio") or post.get("bio") or ""),
            "text_preview": str(post.get("text") or "")[:100],
            "full_text": str(post.get("text") or ""),
            "source": str(post.get("source") or ""),
            "notif_kind": str(post.get("notif_kind") or ""),
            "visited_profile": bool(post.get("visited_profile")),
            "relevance_score": rel_score + fresh_bonus + drop_by_bonus,
        })
    clean_posts.sort(key=lambda item: item["relevance_score"], reverse=True)

    recent_replies = _recent_reply_texts(ledger, max_items=400)
    reply_deduper = _ReplyDeduper(recent_replies)
    follow_heavy = capped.get("follow_profile", 0) >= 50
    reply_heavy = capped.get("reply_post", 0) >= 150
    auto_reply_cap = (
        min(capped["reply_post"], len(buckets["reply_post"]) + 4)
        if (follow_heavy and not reply_heavy and strategy == "balanced")
        else capped["reply_post"]
    )
    replied_urls = {op["url"] for op in buckets["reply_post"]}
    replied_authors: set[str] = set()
    author_likes: dict[str, int] = {}

    for post in clean_posts:
        author_key = post["author"].casefold()
        is_notif = post["source"] == "notification"
        is_visited = post["visited_profile"]
        # Reserve 1 reply per distinct author per wave to feel conversational, not spammy.
        can_reply = (
            auto_reply
            and len(buckets["reply_post"]) < auto_reply_cap
            and post["url"] not in replied_urls
            and author_key not in replied_authors
        )
        drafted: str | None = None
        post_ctx_payload = {
            "text": post["full_text"],
            "author": post["author"],
            "author_bio": post["author_bio"],
            "source": post["source"],
            "notif_kind": post["notif_kind"],
            "visited_profile": is_visited,
        }
        post_analysis: dict[str, Any] | None = None
        if can_reply:
            drafted = draft_friendly_reply(
                post_ctx_payload,
                persona_memory=persona_memory,
                recent_replies=reply_deduper,
                rng=rng,
            )
            if drafted:
                post_analysis = analyze_target_post(post_ctx_payload, persona_memory=persona_memory)

        tech_score, is_elite_tech, tech_reasons = score_technical_quality(
            {
                "text": post["full_text"],
                "author_bio": post["author_bio"],
                "source": post["source"],
                "notif_kind": post["notif_kind"],
            },
            persona_topics=persona_topics,
        )

        # Drop-by warmth: allow up to 2 likes for visited/early-stage neighbors, 1 per unvisited author first.
        cur_likes = author_likes.get(author_key, 0)
        max_likes_for_author = 2 if (is_visited or stage in {"light_engage", "content_build"}) else 1
        if len(buckets["like_post"]) < capped["like_post"] and (
            drafted is not None or cur_likes < max_likes_for_author or len(clean_posts) <= 3
        ):
            author_likes[author_key] = cur_likes + 1
            buckets["like_post"].append({
                "action": "like_post",
                "url": post["url"],
                "author": post["author"],
                "text_preview": post["text_preview"],
                "source": post["source"],
                "visited_before_engage": is_visited or is_notif,
            })

        # Strictly gate reposts to substantive elite technical content ("精品技术内容"),
        # decoupled from whether a reply was drafted, and keep reposts rare per wave.
        if (
            not is_notif
            and len(buckets["repost_post"]) < capped["repost_post"]
            and (not require_quality_repost or is_elite_tech)
        ):
            buckets["repost_post"].append({
                "action": "repost_post",
                "url": post["url"],
                "author": post["author"],
                "text_preview": post["text_preview"],
                "quality_score": tech_score,
                "tech_reasons": tech_reasons,
            })

        if drafted:
            recent_replies.append(drafted)
            reply_deduper.add(drafted)
            replied_urls.add(post["url"])
            replied_authors.add(author_key)
            reply_op: dict[str, Any] = {
                "action": "reply_post",
                "url": post["url"],
                "author": post["author"],
                "text": drafted,
                "auto_drafted": True,
                "source": post["source"],
                "notif_kind": post["notif_kind"],
                "visited_before_engage": is_visited or is_notif,
            }
            if post_analysis:
                reply_op["post_analysis"] = {
                    "author_state": post_analysis["author_state"],
                    "care_angle": post_analysis["care_angle"],
                    "brief_summary": post_analysis["brief_summary"],
                }
            buckets["reply_post"].append(reply_op)

    # 3. Deduplicated & scored follow candidates shaped by active orchestration strategy.
    ranked_cands = sorted(
        [c for c in (follow_candidates or []) if isinstance(c, dict)],
        key=lambda c: score_follow_candidate(c, strategy=strategy),
        reverse=True,
    )
    engaged_authors_this_wave = {
        str(op.get("author") or "").strip().lstrip("@").casefold()
        for op in (buckets["reply_post"] + buckets["like_post"])
        if str(op.get("author") or "").strip()
    }
    max_cold_magnet_follows = max(2, capped["follow_profile"] // 4) if capped["follow_profile"] > 0 else 0
    cold_magnet_added = 0
    local_seen_handles = set(seen_handles)
    for cand in ranked_cands:
        if len(buckets["follow_profile"]) >= capped["follow_profile"]:
            break
        if require_reciprocal_marker and not has_reciprocal_marker(cand):
            continue
        handle = str(cand.get("handle") or cand.get("username") or "").strip().lstrip("@")
        if not _HANDLE_RE.match(handle):
            continue
        key = handle.casefold()
        if key in local_seen_handles:
            continue
        cand_source = str(cand.get("source") or "follow_candidates").lower()
        is_mutual = bool(cand.get("mutual_verified") is True or cand_source in {"notification_follow", "notification_followback"})
        score, _ = score_follow_candidate(cand, strategy=strategy)
        if strategy == "reply_then_follow" and not is_mutual:
            # In reply_then_follow mode, hold off cold follows and let inbound followbacks drive follows.
            continue
        if strategy == "thread_reply_magnet" and not is_mutual:
            # Organic content/reply magnet keeps cold follows rare and restricted to high-affinity peers.
            if score < 35 and key not in engaged_authors_this_wave:
                continue
            if cold_magnet_added >= max_cold_magnet_follows:
                continue
            cold_magnet_added += 1
        if strategy == "niche_sniper_bundle" and not is_mutual and key not in engaged_authors_this_wave:
            # Targeted peer trio focuses on authors engaged in this wave via like/reply first.
            if engaged_authors_this_wave:
                continue
        local_seen_handles.add(key)
        buckets["follow_profile"].append({
            "action": "follow_profile",
            "handle": handle,
            "profile_url": f"https://x.com/{handle}",
            "candidate_score": score,
            "source": str(cand.get("source") or "follow_candidates"),
            "visited_before_engage": bool(cand.get("visited_profile")),
        })

    # Interleave: threads/posts first, then coordinated inbound & outbound interaction bundles,
    # then natural micro-wave pacing over any remaining standalone likes/follows/reposts.
    ordered: list[dict[str, Any]] = []
    for seq in thread_sequences:
        ordered.extend(seq)
    if buckets["publish_post"]:
        lead_posts = 2 if strategy == "thread_reply_magnet" else 1
        for _ in range(min(lead_posts, len(buckets["publish_post"]))):
            ordered.append(buckets["publish_post"].pop(0))

    bundled_ops = _bundle_author_interactions(buckets, interaction_strategy=strategy)
    inbound_bundled = [
        op for op in bundled_ops
        if str(op.get("interaction_role") or "").startswith("inbound_")
    ]
    outbound_bundled = [
        op for op in bundled_ops
        if str(op.get("interaction_role") or "") == "outbound_bundle"
    ]
    ordered.extend(inbound_bundled)

    # Group outbound author bundles cleanly so follow_heavy micro-waves interleave warm bundles
    # without starving follows behind 10+ consecutive like+reply pairs.
    outbound_groups: list[list[dict[str, Any]]] = []
    cur_group: list[dict[str, Any]] = []
    cur_author = ""
    for op in outbound_bundled:
        op_author = str(op.get("author") or op.get("handle") or "").strip().lstrip("@").casefold()
        if cur_group and op_author != cur_author:
            outbound_groups.append(cur_group)
            cur_group = [op]
            cur_author = op_author
        else:
            cur_group.append(op)
            cur_author = op_author
    if cur_group:
        outbound_groups.append(cur_group)

    follow_first_wave = follow_heavy and (not reply_heavy) and strategy in {"reciprocal_first", "balanced"}
    if not follow_first_wave:
        for grp in outbound_groups:
            ordered.extend(grp)
        outbound_groups = []

    buckets["repost_post"].sort(key=lambda item: int(item.get("quality_score") or 0), reverse=True)

    engagement_rich = (like_goal >= 80 or reply_goal >= 30 or strategy in {"thread_reply_magnet", "niche_sniper_bundle"})
    ops_since_repost = 6
    if reply_heavy:
        order_cycle = ["reply_post", "reply_post", "like_post", "reply_post", "follow_profile", "publish_post", "repost_post"]
    elif strategy == "thread_reply_magnet":
        order_cycle = ["reply_post", "like_post", "publish_post", "repost_post", "follow_profile"]
    else:
        order_cycle = ["like_post", "reply_post", "follow_profile", "repost_post", "publish_post"]
    while any(buckets.values()) or outbound_groups:
        if follow_first_wave and (buckets["follow_profile"] or outbound_groups):
            if buckets["follow_profile"]:
                max_burst = 3 if engagement_rich else rng.randint(4, 5)
                follow_burst = min(len(buckets["follow_profile"]), max_burst)
                for _ in range(follow_burst):
                    ordered.append(buckets["follow_profile"].pop(0))
                    ops_since_repost += 1
            if outbound_groups:
                grp = outbound_groups.pop(0)
                ordered.extend(grp)
                ops_since_repost += len(grp)
                if engagement_rich and buckets["like_post"]:
                    ordered.append(buckets["like_post"].pop(0))
                    ops_since_repost += 1
            else:
                secondary = [
                    k for k in ("like_post", "reply_post", "repost_post", "publish_post")
                    if buckets[k] and (k != "repost_post" or ops_since_repost >= 8)
                ]
                if secondary:
                    pick = "like_post" if (engagement_rich and "like_post" in secondary) else rng.choice(secondary)
                    ordered.append(buckets[pick].pop(0))
                    ops_since_repost = 0 if pick == "repost_post" else ops_since_repost + 1
            continue
        for grp in outbound_groups:
            ordered.extend(grp)
            ops_since_repost += len(grp)
        outbound_groups = []
        rng.shuffle(order_cycle)
        for kind in order_cycle:
            if not buckets[kind]:
                continue
            if kind == "repost_post":
                non_repost_left = sum(len(buckets[k]) for k in ("like_post", "reply_post", "follow_profile", "publish_post"))
                if non_repost_left > 0 and ops_since_repost < 8:
                    continue
                ordered.append(buckets["repost_post"].pop(0))
                ops_since_repost = 0
                continue
            if (not reply_heavy) and kind in {"like_post", "follow_profile"} and len(buckets[kind]) >= 2 and rng.random() < 0.35:
                burst = 2
            elif reply_heavy and kind == "like_post" and len(buckets[kind]) >= 2 and rng.random() < 0.25:
                burst = 2
            else:
                burst = 1
            for _ in range(burst):
                if buckets[kind]:
                    ordered.append(buckets[kind].pop(0))
                    ops_since_repost += 1

    operations: list[dict[str, Any]] = []
    total_delay = 0
    seen_standalone_publish = False
    for idx, op in enumerate(ordered):
        cfg = prof["actions"][op["action"]]
        min_gap = int(cfg.get("min_gap_seconds", 60))
        max_gap = max(min_gap, int(cfg.get("max_gap_seconds", min_gap)))
        if idx == 0:
            delay = 0
        else:
            mode_gap = min_gap + (max_gap - min_gap) * 0.35
            base_delay = int(round(rng.triangular(min_gap, max_gap, mode_gap)))
            if follow_heavy:
                extra_pause = rng.randint(10, 25) if rng.random() < 0.15 else 0
            else:
                extra_pause = rng.randint(15, 45) if rng.random() < 0.25 else 0
            delay = base_delay + extra_pause
            # Space consecutive standalone original posts at least 30m apart to avoid velocity spam filters.
            if op["action"] == "publish_post" and not op.get("thread_id"):
                if seen_standalone_publish:
                    min_pub_gap = int(X_RATE_LIMIT_REFERENCE["publish_post"]["min_standalone_gap_seconds"])
                    delay = max(delay, min_pub_gap)
        if op["action"] == "publish_post" and not op.get("thread_id"):
            seen_standalone_publish = True
        total_delay += delay
        operations.append({
            "step_index": idx + 1,
            **op,
            "delay_before_seconds": delay,
        })

    counts = {
        kind: sum(1 for op in operations if op["action"] == kind)
        for kind in ("publish_post", "like_post", "follow_profile", "repost_post", "reply_post")
    }
    wave_preview = slice_wave_for_rate_limits(operations, health.get("rolling_windows"), wave_limit=12)
    return {
        "status": "planned_not_executed",
        "blocked": False,
        "stage": stage,
        "interaction_strategy": strategy,
        "strategy_requested": strategy_requested,
        "strategy_reason": strategy_reason,
        "plan_id": "daily_" + secrets.token_hex(6),
        "health": health,
        "rolling_windows": health.get("rolling_windows"),
        "counts": counts,
        "bundled_count": len(bundled_ops),
        "operation_count": len(operations),
        "operations": operations,
        "wave_safe_preview": {
            "count": len(wave_preview["operations"]),
            "skipped_by_rate_limit": wave_preview["skipped_by_rate_limit"],
            "next_retry_epoch": wave_preview["next_retry_epoch"],
        },
        "estimated_duration_seconds": total_delay + len(operations) * 10,
    }
