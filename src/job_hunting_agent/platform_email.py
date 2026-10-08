"""SMTP-only worker for Java-owned verification jobs."""

from .account_lifecycle import AccountEmailSender
from .platform_auth import PlatformAuthClient


def deliver_platform_verification(
    client: PlatformAuthClient, sender: AccountEmailSender, job_id: int
) -> dict:
    claim = client.email_verification("claim", id=job_id)["claim"]
    if claim is None:
        return {"outbox_id": job_id, "status": "not_claimed"}
    sent = True
    error_type = None
    try:
        sender.send_verification(claim["recipient_email"], claim["action_url"])
    except Exception:  # noqa: BLE001 - SMTP implementations vary; never persist exception text.
        sent = False
        error_type = "SmtpDeliveryFailed"
    result = client.email_verification(
        "finish",
        id=job_id,
        claim_key=claim["claim_key"],
        sent=sent,
        error_type=error_type,
    )
    return {
        "outbox_id": job_id,
        "status": "sent" if sent else "retrying",
        "accepted": result["ok"],
    }
