"""Pricing + cost tracker + TokenGuard auto-poor-mode.

Pricing is per-1M-tokens, [input_$, output_$]. Fuzzy-match by substring so
"claude-sonnet-4-5" matches the "claude-sonnet" entry without exact version.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PRICING: dict[str, tuple[float, float]] = {
    # in $/M tokens, [input, output]
    "claude-sonnet":  (3.00, 15.00),
    "claude-opus":    (15.00, 75.00),
    "claude-haiku":   (0.80, 4.00),
    "deepseek-chat":  (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "gpt-4o":         (2.50, 10.00),
    "gpt-4o-mini":    (0.15, 0.60),
    "gemini-2.5-pro": (1.25, 10.00),
    "grok-3-fast":    (0.20, 0.40),
    "grok-3":         (3.00, 15.00),
}


def lookup(model: str) -> tuple[float, float]:
    """Fuzzy match: longest matching key wins. Returns (0,0) on miss."""
    m = model.lower()
    best: tuple[str, tuple[float, float]] | None = None
    for k, v in PRICING.items():
        if k in m and (best is None or len(k) > len(best[0])):
            best = (k, v)
    return best[1] if best else (0.0, 0.0)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = lookup(model)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


@dataclass
class CostTracker:
    """Per-agent running cost + TokenGuard state."""
    by_agent: dict[str, dict] = field(default_factory=dict)  # name → {in, out, cost, breaches}

    def add(self, agent: str, model: str, input_tokens: int, output_tokens: int) -> dict:
        slot = self.by_agent.setdefault(
            agent, {"in": 0, "out": 0, "cost": 0.0, "breaches": 0, "model": model}
        )
        slot["in"] += input_tokens
        slot["out"] += output_tokens
        slot["cost"] = estimate_cost(model, slot["in"], slot["out"])
        slot["model"] = model
        return slot

    def total_cost(self) -> float:
        return sum(s["cost"] for s in self.by_agent.values())

    def summary(self) -> str:
        if not self.by_agent:
            return "no usage yet"
        rows = []
        for name, s in self.by_agent.items():
            rows.append(
                f"{name}: in={s['in']} out={s['out']} ${s['cost']:.4f} "
                f"({s['model']})"
            )
        rows.append(f"total: ${self.total_cost():.4f}")
        return "  |  ".join(rows)


@dataclass
class TokenGuard:
    """Trip Poor Mode after N consecutive turns whose output exceeds threshold."""
    threshold: int = 2000
    consecutive_needed: int = 3
    consecutive: int = 0
    tripped: bool = False

    def observe(self, output_tokens: int) -> bool:
        """Returns True the moment poor mode is freshly tripped."""
        if self.tripped:
            return False
        if output_tokens > self.threshold:
            self.consecutive += 1
            if self.consecutive >= self.consecutive_needed:
                self.tripped = True
                return True
        else:
            self.consecutive = 0
        return False

    def reset(self) -> None:
        self.consecutive = 0
        self.tripped = False
