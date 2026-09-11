from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class StreamMetric:
    timestamp: float
    path: str
    range_header: Optional[str]
    ttfb_ms: float
    status_code: int


class TelemetryTracker:
    def __init__(self, max_history: int = 500):
        self.max_history = max_history
        self._metrics: List[StreamMetric] = []

    def record_range_request(
        self,
        path: str,
        range_header: Optional[str],
        ttfb_ms: float,
        status_code: int = 206,
    ) -> None:
        metric = StreamMetric(
            timestamp=time.time(),
            path=path,
            range_header=range_header,
            ttfb_ms=ttfb_ms,
            status_code=status_code,
        )
        self._metrics.append(metric)
        if len(self._metrics) > self.max_history:
            self._metrics = self._metrics[-self.max_history :]

    def get_summary(self) -> dict:
        if not self._metrics:
            return {
                "total_requests": 0,
                "avg_ttfb_ms": 0.0,
                "min_ttfb_ms": 0.0,
                "max_ttfb_ms": 0.0,
                "recent_requests": [],
            }

        ttfbs = [m.ttfb_ms for m in self._metrics]
        return {
            "total_requests": len(self._metrics),
            "avg_ttfb_ms": round(sum(ttfbs) / len(ttfbs), 2),
            "min_ttfb_ms": round(min(ttfbs), 2),
            "max_ttfb_ms": round(max(ttfbs), 2),
            "recent_requests": [
                {
                    "timestamp": m.timestamp,
                    "path": m.path,
                    "range": m.range_header,
                    "ttfb_ms": m.ttfb_ms,
                    "status": m.status_code,
                }
                for m in self._metrics[-10:]
            ],
        }

    def clear(self) -> None:
        self._metrics.clear()


telemetry_tracker = TelemetryTracker()
