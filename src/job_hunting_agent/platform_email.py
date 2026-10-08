"""SMTP-only worker for Java-owned account-action emails."""

from .account_lifecycle import AccountEmailSender
from .platform_auth import PlatformAuthClient


def deliver_platform_verification(
    client: PlatformAuthClient, sender: AccountEmailSender, job_id: int
) -> dict:
    return deliver_platform_account_email(client, sender, job_id, "verify_email")


def deliver_platform_account_email(
    client: PlatformAuthClient, sender: AccountEmailSender, job_id: int, purpose: str
) -> dict:
    if purpose not in {"verify_email", "reset_password"}:
        raise ValueError("Unknown account email purpose")
    action = client.email_verification if purpose == "verify_email" else client.password_reset
    claim = action("claim", id=job_id)["claim"]
    if claim is None:
        return {"outbox_id": job_id, "status": "not_claimed"}
    sent = True
    error_type = None
    try:
        send = sender.send_verification if purpose == "verify_email" else sender.send_password_reset
        send(claim["recipient_email"], claim["action_url"])
    except Exception:  # noqa: BLE001 - SMTP implementations vary; never persist exception text.
        sent = False
        error_type = "SmtpDeliveryFailed"
    result = action(
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
