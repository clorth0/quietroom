"""Priority/interval band scheduler: one receiver, explicit revisit cadence."""

from __future__ import annotations


class BandScheduler:
    def __init__(self, segments):
        self.segments = [s for s in segments if s.enabled]

    def due(self, now, last_run):
        due = []
        for s in self.segments:
            last = last_run.get(s.name)
            if last is None or now - last >= s.interval_s:
                overdue = 999999.0 if last is None else (now - last) / s.interval_s
                due.append((overdue, s.priority, s))
        due.sort(key=lambda x: (-x[0], -x[1]))
        return [s for _, _, s in due]
