"""
Shared alerting state: thin wrapper around NoiseReducer for access
from both the alerting runner and the /status API endpoint.
"""
from __future__ import annotations

from src.alerting.models import Incident, NotificationRecord
from src.alerting.noise_reduction import NoiseReducer


class AlertingState:
    def __init__(self) -> None:
        self.noise_reducer = NoiseReducer()

    def get_active_incidents(self) -> list[Incident]:
        return self.noise_reducer.get_active_incidents()

    def get_recent_alerts(self, limit: int = 20) -> list[NotificationRecord]:
        return self.noise_reducer.get_recent_alerts(limit=limit)
