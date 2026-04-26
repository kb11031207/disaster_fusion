"""
SNS alerting for high-priority fused findings.

Fires after fusion completes — scans for critical findings and sends
a consolidated notification to subscribed analysts/operators.

Alert triggers:
  - unreported_damage + severity in (severe, destroyed)  → HIGH
  - unreported_damage + facility_type is infrastructure  → HIGH
  - conflicting_severity on any finding                  → MEDIUM

Requires SNS_TOPIC_ARN in environment. Silently skips if not set.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

import boto3


_INFRASTRUCTURE_TYPES = {
    "infrastructure_bridge",
    "transit_station",
    "infrastructure",
    "utility",
}

_SEVERE_SEVERITIES = {"severe", "destroyed"}


def _classify(finding: dict) -> Optional[tuple[str, str]]:
    """
    Return (priority, rule_name) if this finding triggers an alert, else None.
    Checks HIGH-priority rules first.
    """
    status   = finding.get("fusion_status", "")
    severity = finding.get("final_severity", "")
    ftype    = finding.get("facility_type", "")

    if status == "unreported_damage" and severity in _SEVERE_SEVERITIES:
        return ("HIGH", "unreported_severe")

    if status == "unreported_damage" and ftype in _INFRASTRUCTURE_TYPES:
        return ("HIGH", "unreported_infrastructure")

    if status == "conflicting_severity":
        return ("MEDIUM", "conflicting_severity")

    return None


def _format_finding(finding: dict, rule_name: str) -> str:
    name     = finding.get("entity_name", "Unknown location")
    fid      = finding.get("id", "?")
    severity = finding.get("final_severity", "unknown")

    if rule_name == "unreported_severe":
        summary = ""
        if finding.get("video"):
            summary = (finding["video"].get("summary") or "")[:180]
        return f"[{fid}] UNREPORTED {severity.upper()} DAMAGE: {name}\n        Video: {summary}"

    if rule_name == "unreported_infrastructure":
        ftype = finding.get("facility_type", "infrastructure")
        return f"[{fid}] UNREPORTED INFRASTRUCTURE DAMAGE ({ftype}): {name}"

    if rule_name == "conflicting_severity":
        report_sev = "unknown"
        if finding.get("pdf"):
            report_sev = finding["pdf"].get("claimed_severity", "unknown")
        return (
            f"[{fid}] SEVERITY CONFLICT: {name}\n"
            f"        Video: {severity}  |  Report: {report_sev}"
        )

    return f"[{fid}] {name}"


def check_and_alert(
    findings:   list[dict],
    event_name: str = "Disaster Assessment",
    location:   str = "",
) -> dict:
    """
    Scan frontend-schema findings for alert-worthy conditions and send
    one consolidated SNS message if any are found.

    Args:
        findings:   list of Finding dicts (master_findings shape)
        event_name: human-readable event label for the alert subject
        location:   optional location string for context

    Returns:
        {
          "alerts_triggered": int,
          "alerts_sent":      bool,
          "message_id":       str | None,
          "triggered":        list[dict]   # per-finding detail
        }
    """
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "").strip()
    if not topic_arn:
        print("SNS_TOPIC_ARN not set — skipping alerts")
        return {"alerts_triggered": 0, "alerts_sent": False, "message_id": None, "triggered": []}

    triggered = []
    for f in findings:
        result = _classify(f)
        if result:
            priority, rule = result
            triggered.append({
                "finding_id": f.get("id"),
                "priority":   priority,
                "rule":       rule,
                "message":    _format_finding(f, rule),
            })

    if not triggered:
        print("No critical findings — no alert sent.")
        return {"alerts_triggered": 0, "alerts_sent": False, "message_id": None, "triggered": []}

    high   = [t for t in triggered if t["priority"] == "HIGH"]
    medium = [t for t in triggered if t["priority"] == "MEDIUM"]

    now     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"DisasterFusion Alert: {len(triggered)} critical findings — {event_name}"[:100]

    lines = [
        f"DisasterFusion Automated Alert",
        f"Event:     {event_name}" + (f" | {location}" if location else ""),
        f"Generated: {now}",
        f"Findings:  {len(triggered)} alert(s) — {len(high)} HIGH, {len(medium)} MEDIUM",
        "",
        "=" * 60,
    ]

    if high:
        lines.append("\nHIGH PRIORITY:")
        for t in high:
            lines.append(f"  {t['message']}")

    if medium:
        lines.append("\nMEDIUM PRIORITY:")
        for t in medium:
            lines.append(f"  {t['message']}")

    lines += [
        "",
        "=" * 60,
        "Open the DisasterFusion dashboard to view evidence chains and map.",
        "— DisasterFusion Automated Alert System",
    ]

    body = "\n".join(lines)

    try:
        sns  = boto3.client("sns", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        resp = sns.publish(TopicArn=topic_arn, Subject=subject, Message=body)
        mid  = resp["MessageId"]
        print(f"Alert sent — {len(triggered)} findings. MessageId: {mid}")
        return {
            "alerts_triggered": len(triggered),
            "alerts_sent":      True,
            "message_id":       mid,
            "triggered":        triggered,
        }
    except Exception as e:
        print(f"SNS publish failed: {e}")
        return {
            "alerts_triggered": len(triggered),
            "alerts_sent":      False,
            "message_id":       None,
            "error":            str(e),
            "triggered":        triggered,
        }
