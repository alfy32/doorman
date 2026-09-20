"""Sending the one email this app needs, without running a mail server.

Delivery goes through an SMTP account you already have -- a Gmail address with
an app password, or any provider's relay. Nothing is hosted here.

If no SMTP is configured the link is written to the server log instead, so the
app still works on day one: you read the link and pass it to the person
yourself. That is a fallback, not verification -- it proves nothing about who
owns the address, so configure SMTP before anyone else signs up.
"""
import logging, smtplib, ssl
from email.message import EmailMessage

log = logging.getLogger("doorman.mail")


class MailError(RuntimeError):
    pass


def configured(cfg):
    return bool((cfg or {}).get("host") and (cfg or {}).get("from"))


def send(cfg, to, subject, body):
    """Send one plain-text message. Returns True if it actually went out."""
    cfg = cfg or {}
    if not configured(cfg):
        log.warning("SMTP not configured — message for %s not sent:\n%s", to, body)
        return False

    msg = EmailMessage()
    msg["From"] = cfg["from"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    host, port = cfg["host"], int(cfg.get("port") or 587)
    user, password = cfg.get("username"), cfg.get("password")
    try:
        if int(port) == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(),
                                  timeout=20) as s:
                if user:
                    s.login(user, password or "")
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                if user:
                    s.login(user, password or "")
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"could not send to {to}: {e}") from e
    log.info("sent %r to %s", subject, to)
    return True
