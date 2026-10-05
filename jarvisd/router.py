"""Gate 2 and 3 input: the router contract and the v1 StubRouter (design section 6).

Layer L2. Imports config and models only. It must stay that way: a router sees item text
for every item that passed the tier gate, before the importance gate decides whether Claude
may see it.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from jarvisd.config import Config, ConfigError
from jarvisd.models import Item, Language, RouterDecision

# Only this much text is scanned: a regex over a huge body is a stall, not a signal.
_SCAN_LIMIT = 20_000
# Rule bucket (config key, underscore) to the category name that [gates].importance_escalate uses.
_BUCKETS: tuple[tuple[str, str], ...] = (
    ("financial", "financial"),
    ("client_facing", "client-facing"),
    ("irreversible", "irreversible"),
    ("work_prod", "work-prod"),
)

_WORD = re.compile(r"[^\W_]+", re.UNICODE)

# Stopwords picked to be unambiguous between the three languages (no "a", "de", "en", "fin", "ma").
_EN = frozenset(
    "the and is are was were to of for with in on this that it be from have has not will we you they "
    "at by or an as if our your their".split()
)
_FR = frozenset(
    "le la les des une est et pour avec dans sur pas que qui du au aux ce cette ont sont je nous vous "
    "il elle ne mais ou donc être été très plus par".split()
)
_DARIJA = frozenset(
    "wach wash kifach kif bghit bghina bghiti mashi machi daba dyal diyal chno shno bzaf mzyan khdma "
    "safi inchallah hamdullah wakha walo hna ntouma nta nti labas bikhir lyoum lyom ghda sbah ghadi "
    "kayn kayna khassni 3la 3and 7ta 9bel 3lach 7it".split()
)


@runtime_checkable
class Router(Protocol):
    """Classifies one item that already passed the tier gate.

    ROUTERS MUST BE LOCAL. A router reads item title and text before the importance gate has
    decided whether the item may leave the machine, so a Claude-backed (or any network-backed)
    router would send unreviewed text out. That is forbidden. A real router (phase 1, a small
    local model) plugs in by registering a factory in ROUTERS and setting [router].adapter.
    """

    def classify(self, item: Item) -> RouterDecision:
        """Return the seven-field decision. Must not raise for any well-formed Item."""
        ...


RouterFactory = Callable[[Config], Router]


def detect_language(text: str) -> Language:
    """Stopword vote. Short texts need one hit, longer ones two, so a stray word is not a verdict."""
    tokens = [t.casefold() for t in _WORD.findall(text[:_SCAN_LIMIT])]
    if not tokens:
        return "other"
    scores: dict[Language, int] = {
        "en": sum(t in _EN for t in tokens),
        "fr": sum(t in _FR for t in tokens),
        "ar-darija-latin": sum(t in _DARIJA for t in tokens),
    }
    best = max(scores, key=lambda k: scores[k])
    needed = 1 if len(tokens) <= 4 else 2
    if scores[best] < needed:
        return "other"
    # A tie between languages is not evidence for either.
    if sum(1 for v in scores.values() if v == scores[best]) > 1:
        return "other"
    return best


class StubRouter:
    """Deterministic router: regex importance buckets, zero confidence, never sensitive.

    Sensitive is always False because gate 1 already ran and the router only sees items that
    passed it. Confidence comes from config (0.0), so every item reaches the real gate-3 path.
    """

    def __init__(self, cfg: Config) -> None:
        rules = cfg.router.stub.rules
        self._confidence = cfg.router.stub.confidence
        self._rules: list[tuple[str, list[re.Pattern[str]]]] = [
            (category, [re.compile(p) for p in getattr(rules, key)]) for key, category in _BUCKETS
        ]

    def _bucket(self, haystack: str) -> str | None:
        for category, patterns in self._rules:
            if any(p.search(haystack) for p in patterns):
                return category
        return None

    def classify(self, item: Item) -> RouterDecision:
        haystack = f"{item.title}\n{item.text}"[:_SCAN_LIMIT]
        bucket = self._bucket(haystack)
        if bucket is not None or item.work:
            importance = "high"
        elif item.kind == "active_task":
            importance = "med"
        else:
            importance = "low"
        return RouterDecision(
            category=bucket or item.kind or item.source or "unknown",
            sensitive=False,
            importance=importance,
            confidence=self._confidence,
            needs_tools=[],
            language=detect_language(haystack),
            reason="local_tier_not_installed",
        )


ROUTERS: dict[str, RouterFactory] = {"stub": StubRouter}


def build_router(cfg: Config) -> Router:
    """Instantiate the router named by [router].adapter. An unknown name is a config error."""
    factory = ROUTERS.get(cfg.router.adapter)
    if factory is None:
        known = ", ".join(sorted(ROUTERS))
        raise ConfigError(f"unknown router adapter {cfg.router.adapter!r} (known: {known})")
    return factory(cfg)
