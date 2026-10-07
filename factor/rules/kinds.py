"""Alert kinds that share session management (Reaktor SL/TP, flatten hour)."""

from __future__ import annotations

# Asia box engine: breakout and Asia Overnight H (own preset, same payload).
ASIA_BOX_KINDS = frozenset({"asia_breakout", "asia_breakout_h"})

# Session entries: SL/TP from the webhook, breakeven off, flatten from the alert.
SESSION_KINDS = frozenset({"asia_breakout", "asia_breakout_h", "dawn_cross"})


def is_asia_box(kind: str) -> bool:
    return str(kind or "").strip() in ASIA_BOX_KINDS


def is_session_managed(kind: str) -> bool:
    return str(kind or "").strip() in SESSION_KINDS
