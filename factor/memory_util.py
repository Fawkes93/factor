"""
Process memory helpers for Render 512MB plans.

Linux: /proc/self/status VmRSS. Fallback: resource.ru_maxrss.
"""

from __future__ import annotations

import gc
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

try:
    import resource
except ImportError:  # Windows
    resource = None  # type: ignore


def rss_bytes() -> Optional[int]:
    """Current resident set size in bytes, or None if unknown."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except Exception:
        pass
    if resource is None:
        return None
    try:
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return int(ru)
        return int(ru) * 1024  # KiB on Linux
    except Exception:
        return None


def rss_mb() -> Optional[float]:
    b = rss_bytes()
    if b is None:
        return None
    return round(b / (1024 * 1024), 1)


def memory_snapshot() -> Dict[str, Any]:
    """Lightweight stats for /api/health and logs."""
    mb = rss_mb()
    out: Dict[str, Any] = {
        "rss_mb": mb,
        "gc_counts": list(gc.get_count()),
        "pid": os.getpid(),
    }
    try:
        out["threads"] = threading.active_count()
    except Exception:
        pass
    return out


def maybe_gc(warn_mb: float = 380.0) -> int:
    """Run gc.collect if RSS is high; return objects collected (0 if skipped)."""
    mb = rss_mb()
    if mb is not None and mb >= warn_mb:
        return gc.collect()
    return 0


def memory_watch_loop(
    stop_event: threading.Event,
    *,
    interval_sec: float = 900.0,
    warn_mb: float = 380.0,
) -> None:
    """
    Log RSS periodically so Render logs show growth before OOM.
    Gentle gc.collect() when above warn_mb.
    """
    if stop_event.wait(60):
        return
    while not stop_event.is_set():
        try:
            snap = memory_snapshot()
            mb = snap.get("rss_mb")
            thr = snap.get("threads")
            line = f"🧠 Memory: rss={mb}MB threads={thr} gc={snap.get('gc_counts')}"
            if mb is not None and mb >= warn_mb:
                collected = gc.collect()
                line += f" · gc.collect()→{collected}"
                print(f"⚠️  {line}", flush=True)
            else:
                print(line, flush=True)
        except Exception as e:
            print(f"⚠️  memory watch error: {e}", flush=True)
        if stop_event.wait(interval_sec):
            break
