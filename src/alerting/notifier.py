"""
Notifier: delivers alert messages over configured channels.

Channels:
  - Console (always active – logs to stdout)
  - Slack   (if SLACK_WEBHOOK_URL is set)
  - Email   (if EMAIL_USER and EMAIL_TO are set)

All remote channels use retry with exponential backoff.
"""
from __future__ import annotations

import json
import logging
import smtplib
import time
from email.mime.text import MIMEText
from typing import Optional

import httpx

from config import settings
from src.alerting.models import IncidentStatus

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0   # seconds


class Notifier:

    def send(
        self,
        *,
        engineering_message: str,
        business_message: str,
        severity: str,
        incident_status: IncidentStatus,
        service: str,
        endpoint: Optional[str],
        pattern_type: str,
    ) -> list[str]:
        """
        Send notifications over all configured channels.
        Returns list of channel names that succeeded.
        """
        channels_sent: list[str] = []

        # Console (always)
        self._console(engineering_message, business_message, severity)
        channels_sent.append("console")

        # Slack (optional)
        if settings.SLACK_WEBHOOK_URL:
            ok = self._slack(
                engineering_message=engineering_message,
                business_message=business_message,
                severity=severity,
                status=incident_status,
            )
            if ok:
                channels_sent.append("slack")

        # Email (optional)
        if settings.EMAIL_USER and settings.EMAIL_TO:
            ok = self._email(
                engineering_message=engineering_message,
                business_message=business_message,
                severity=severity,
                service=service,
            )
            if ok:
                channels_sent.append("email")

        return channels_sent

    # ── Console ────────────────────────────────────────────────────────────────

    def _console(
        self, engineering_message: str, business_message: str, severity: str
    ) -> None:
        separator = "─" * 60
        logger.warning(
            "\n%s\n[ALERT %s]\n  Engineering: %s\n  Business:    %s\n%s",
            separator,
            severity,
            engineering_message,
            business_message,
            separator,
        )

    # ── Slack ──────────────────────────────────────────────────────────────────

    def _slack(
        self,
        *,
        engineering_message: str,
        business_message: str,
        severity: str,
        status: IncidentStatus,
    ) -> bool:
        color_map = {"P0": "#FF0000", "P1": "#FFA500", "P2": "#FFFF00"}
        color = color_map.get(severity, "#888888")
        if status == IncidentStatus.RESOLVED:
            color = "#36A64F"

        payload = {
            "attachments": [
                {
                    "color": color,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*[{severity}] Engineering*\n{engineering_message}",
                            },
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Business*\n{business_message}",
                            },
                        },
                    ],
                }
            ]
        }

        return self._post_with_retry(settings.SLACK_WEBHOOK_URL, payload, channel="Slack")

    # ── Email ──────────────────────────────────────────────────────────────────

    def _email(
        self,
        *,
        engineering_message: str,
        business_message: str,
        severity: str,
        service: str,
    ) -> bool:
        subject = f"[{severity}] Alert: {service} – Monitoring System"
        body = (
            f"Engineering Alert:\n{engineering_message}\n\n"
            f"Business Summary:\n{business_message}\n"
        )
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = settings.EMAIL_FROM or settings.EMAIL_USER
        msg["To"] = settings.EMAIL_TO

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with smtplib.SMTP(settings.EMAIL_HOST, settings.EMAIL_PORT) as smtp:
                    smtp.starttls()
                    smtp.login(settings.EMAIL_USER, settings.EMAIL_PASSWORD)
                    smtp.sendmail(msg["From"], [settings.EMAIL_TO], msg.as_string())
                logger.info("Email sent to %s", settings.EMAIL_TO)
                return True
            except Exception as exc:
                backoff = INITIAL_BACKOFF * (2 ** (attempt - 1))
                logger.warning(
                    "Email attempt %d/%d failed: %s (retry in %.1fs)",
                    attempt, MAX_RETRIES, exc, backoff,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(backoff)
        logger.error("Email delivery failed after %d attempts", MAX_RETRIES)
        return False

    # ── Shared HTTP helper ─────────────────────────────────────────────────────

    def _post_with_retry(self, url: str, payload: dict, channel: str) -> bool:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=10) as client:
                    resp = client.post(url, json=payload)
                if resp.status_code < 300:
                    logger.info("%s notification sent (status=%d)", channel, resp.status_code)
                    return True
                logger.warning(
                    "%s returned %d: %s", channel, resp.status_code, resp.text[:200]
                )
            except Exception as exc:
                logger.warning("%s attempt %d/%d failed: %s", channel, attempt, MAX_RETRIES, exc)

            backoff = INITIAL_BACKOFF * (2 ** (attempt - 1))
            if attempt < MAX_RETRIES:
                time.sleep(backoff)

        logger.error("%s delivery failed after %d attempts", channel, MAX_RETRIES)
        return False
