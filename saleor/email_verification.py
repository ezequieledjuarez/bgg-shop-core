"""Token-based registration email verification (PostgreSQL-backed)."""
import datetime
import hashlib
import logging
import math
import re
import secrets
import smtplib
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings
from django.core.mail import send_mail
from django.db import DatabaseError, connection, transaction

logger = logging.getLogger(__name__)

TOKEN_LIFETIME = datetime.timedelta(seconds=3600)
RESEND_COOLDOWN = datetime.timedelta(seconds=120)

SCOPE_RESEND_CALLER = "RESEND_CALLER"
SCOPE_RESEND_RECIPIENT = "RESEND_RECIPIENT"
SCOPE_INVALID_CONFIRM = "INVALID_CONFIRM_CALLER"

# scope -> (maximum events in window, window)
LIMITS = {
    SCOPE_RESEND_CALLER: (10, datetime.timedelta(seconds=3600)),
    SCOPE_RESEND_RECIPIENT: (2, datetime.timedelta(seconds=3600)),
    SCOPE_INVALID_CONFIRM: (10, datetime.timedelta(seconds=900)),
}

_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{43}\Z")
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_EMAIL_RE = re.compile(r"\A[^@\s]+@[^@\s]+\Z")
MAX_EMAIL_LENGTH = 254

STATE_PENDING = "PENDING"
STATE_VERIFIED = "VERIFIED"
STATE_EXEMPT = "LEGACY_EXEMPT"

DELIVERY_SUBMITTED = "SUBMITTED"
DELIVERY_UNCONFIRMED = "UNCONFIRMED"
DELIVERY_FAILED = "FAILED"

RESEND_MESSAGE = (
    "If the account is waiting for verification, a new link has been requested."
)
CONFIRM_MESSAGE = "Your email address has been verified. You can now sign in."


class DuplicateEmail(Exception):
    pass


@dataclass(frozen=True)
class Registration:
    user: object
    token: str


@dataclass(frozen=True)
class ConfirmResult:
    verified: bool = False
    rate_limited: bool = False
    retry_after: int = 0


@dataclass(frozen=True)
class ResendResult:
    invalid: bool = False
    rate_limited: bool = False
    retry_after: int = 0
    issued: "Registration | None" = None


def normalize_email(value):
    return value.strip().casefold()


def is_valid_email(value):
    return 0 < len(value) <= MAX_EMAIL_LENGTH and bool(_EMAIL_RE.match(value))


def is_valid_token(value):
    return isinstance(value, str) and bool(_TOKEN_RE.match(value))


def is_valid_caller(value):
    return isinstance(value, str) and bool(_DIGEST_RE.match(value))


def new_token():
    return secrets.token_urlsafe(32)


def token_digest(token):
    return hashlib.sha256(f"email-verification:v1:{token}".encode()).hexdigest()


def email_digest(email):
    return hashlib.sha256(f"email-verification:recipient:v1:{email}".encode()).hexdigest()


def _db_now(cursor):
    cursor.execute("SELECT clock_timestamp()")
    return cursor.fetchone()[0]


def _ceil_seconds(delta):
    return max(1, math.ceil(delta.total_seconds()))


def _lock_bucket(cursor, scope, subject):
    cursor.execute(
        "INSERT INTO email_verification_limit_bucket (scope, subject_digest) "
        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
        [scope, subject],
    )
    cursor.execute(
        "SELECT 1 FROM email_verification_limit_bucket "
        "WHERE scope = %s AND subject_digest = %s FOR UPDATE",
        [scope, subject],
    )


def _window_state(cursor, scope, subject, now):
    """Prune expired events of a locked bucket; return (count, retry_after)."""
    limit, window = LIMITS[scope]
    cursor.execute(
        "DELETE FROM email_verification_limit_event "
        "WHERE scope = %s AND subject_digest = %s AND occurred_at <= %s",
        [scope, subject, now - window],
    )
    cursor.execute(
        "SELECT occurred_at FROM email_verification_limit_event "
        "WHERE scope = %s AND subject_digest = %s ORDER BY occurred_at, id",
        [scope, subject],
    )
    events = [row[0] for row in cursor.fetchall()]
    if len(events) < limit:
        return len(events), 0
    # The window frees a slot when the (count - limit + 1)th oldest event leaves it.
    release = events[len(events) - limit] + window
    return len(events), _ceil_seconds(release - now)


def _record_event(cursor, scope, subject, now):
    cursor.execute(
        "INSERT INTO email_verification_limit_event (scope, subject_digest, occurred_at) "
        "VALUES (%s, %s, %s)",
        [scope, subject, now],
    )


def _issue_challenge(cursor, user_id, email, token, now):
    cursor.execute(
        "INSERT INTO email_verification_challenge "
        "(user_id, token_digest, email_normalized, issued_at, expires_at) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET token_digest = EXCLUDED.token_digest, "
        "email_normalized = EXCLUDED.email_normalized, issued_at = EXCLUDED.issued_at, "
        "expires_at = EXCLUDED.expires_at, consumed_at = NULL, "
        "delivery_status = 'UNCONFIRMED', delivery_finished_at = NULL",
        [user_id, token_digest(token), email, now, now + TOKEN_LIFETIME],
    )


def register_pending(email, password, first_name, last_name):
    """Create user, pending state and first challenge atomically."""
    from django.contrib.auth.models import User
    from django.db import IntegrityError

    email = normalize_email(email)
    token = new_token()
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    [f"email-verification:register:{email}"],
                )
            if User.objects.filter(email__iexact=email).exists():
                raise DuplicateEmail
            user = User.objects.create_user(
                username=email, email=email, password=password,
                first_name=first_name.strip(), last_name=last_name.strip(),
            )
            with connection.cursor() as cursor:
                now = _db_now(cursor)
                cursor.execute(
                    "INSERT INTO account_email_verification (user_id, state) "
                    "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state",
                    [user.pk, STATE_PENDING],
                )
                _issue_challenge(cursor, user.pk, email, token, now)
    except IntegrityError:
        raise DuplicateEmail from None
    return Registration(user=user, token=token)


def _verification_link(token):
    origin = getattr(settings, "EMAIL_VERIFICATION_FRONTEND_ORIGIN", "") or ""
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/") \
            or parts.query or parts.fragment:
        return None
    return f"{parts.scheme}://{parts.netloc}/verify-email#token={token}"


def deliver(user_id, email, token):
    """Send the verification mail after commit; never raises for expected SMTP failures."""
    digest = token_digest(token)
    link = _verification_link(token)
    status = DELIVERY_FAILED
    if link is None:
        logger.error("email verification link origin is not configured")
    else:
        try:
            sent = send_mail(
                "Verify your email address",
                f"Open this link to verify your email address. It expires in one hour.\n\n{link}\n",
                None,
                [email],
                fail_silently=False,
            )
            status = DELIVERY_SUBMITTED if sent else DELIVERY_FAILED
        except (OSError, smtplib.SMTPException):
            logger.warning("email verification delivery failed")
        if status == DELIVERY_FAILED:
            logger.warning("email verification delivery was not submitted")
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE email_verification_challenge SET delivery_status = %s, "
                "delivery_finished_at = %s WHERE user_id = %s AND token_digest = %s "
                "AND consumed_at IS NULL",
                [status, _db_now(cursor), user_id, digest],
            )
    except DatabaseError:
        logger.error("email verification delivery metadata update failed")
    return DELIVERY_SUBMITTED if status == DELIVERY_SUBMITTED else DELIVERY_UNCONFIRMED


def confirm(token, caller):
    """Atomically consume a token; caller is a trusted 64-hex digest."""
    with transaction.atomic(), connection.cursor() as cursor:
        _lock_bucket(cursor, SCOPE_INVALID_CONFIRM, caller)
        now = _db_now(cursor)
        _, retry_after = _window_state(cursor, SCOPE_INVALID_CONFIRM, caller, now)
        if retry_after:
            return ConfirmResult(rate_limited=True, retry_after=retry_after)
        if is_valid_token(token) and _consume(cursor, token_digest(token), now):
            return ConfirmResult(verified=True)
        _record_event(cursor, SCOPE_INVALID_CONFIRM, caller, now)
        return ConfirmResult()


def _consume(cursor, digest, now):
    cursor.execute(
        "SELECT user_id FROM email_verification_challenge WHERE token_digest = %s", [digest]
    )
    row = cursor.fetchone()
    if row is None:
        return False
    user_id = row[0]
    cursor.execute(
        "SELECT state FROM account_email_verification WHERE user_id = %s FOR UPDATE", [user_id]
    )
    sidecar = cursor.fetchone()
    cursor.execute(
        "SELECT consumed_at, expires_at FROM email_verification_challenge "
        "WHERE user_id = %s AND token_digest = %s FOR UPDATE",
        [user_id, digest],
    )
    challenge = cursor.fetchone()
    cursor.execute("SELECT is_active FROM auth_user WHERE id = %s", [user_id])
    user = cursor.fetchone()
    if not (sidecar and challenge and user):
        return False
    consumed_at, expires_at = challenge
    if sidecar[0] != STATE_PENDING or not user[0] or consumed_at is not None or now >= expires_at:
        return False
    cursor.execute(
        "UPDATE email_verification_challenge SET consumed_at = %s WHERE user_id = %s",
        [now, user_id],
    )
    cursor.execute(
        "UPDATE account_email_verification SET state = %s, verified_at = %s WHERE user_id = %s",
        [STATE_VERIFIED, now, user_id],
    )
    return True


def resend(email, caller):
    """Neutral resend. Returns the issued registration only when a mail must be sent."""
    email = normalize_email(email)
    if not is_valid_email(email):
        return ResendResult(invalid=True)
    recipient = email_digest(email)
    with transaction.atomic(), connection.cursor() as cursor:
        _lock_bucket(cursor, SCOPE_RESEND_CALLER, caller)
        now = _db_now(cursor)
        _, retry_after = _window_state(cursor, SCOPE_RESEND_CALLER, caller, now)
        if retry_after:
            return ResendResult(rate_limited=True, retry_after=retry_after)
        _record_event(cursor, SCOPE_RESEND_CALLER, caller, now)
        cursor.execute(
            "SELECT u.id FROM auth_user u JOIN account_email_verification v ON v.user_id = u.id "
            "WHERE LOWER(u.email) = %s AND u.is_active AND v.state = %s",
            [email, STATE_PENDING],
        )
        row = cursor.fetchone()
        if row is None:
            return ResendResult()
        user_id = row[0]
        _lock_bucket(cursor, SCOPE_RESEND_RECIPIENT, recipient)
        cursor.execute(
            "SELECT v.state, u.is_active FROM account_email_verification v "
            "JOIN auth_user u ON u.id = v.user_id WHERE v.user_id = %s FOR UPDATE OF v",
            [user_id],
        )
        locked = cursor.fetchone()
        cursor.execute(
            "SELECT issued_at FROM email_verification_challenge WHERE user_id = %s FOR UPDATE",
            [user_id],
        )
        challenge = cursor.fetchone()
        if not locked or locked[0] != STATE_PENDING or not locked[1]:
            return ResendResult()
        _, recipient_wait = _window_state(cursor, SCOPE_RESEND_RECIPIENT, recipient, now)
        if recipient_wait or (challenge and now < challenge[0] + RESEND_COOLDOWN):
            return ResendResult()
        token = new_token()
        _issue_challenge(cursor, user_id, email, token, now)
        _record_event(cursor, SCOPE_RESEND_RECIPIENT, recipient, now)
    return ResendResult(issued=Registration(user=user_id, token=token))


def is_eligible(user_id):
    """Active and verified or legacy-exempt. Fails closed on any storage problem."""
    try:
        user_id = int(user_id)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM auth_user u JOIN account_email_verification v ON v.user_id = u.id "
                "WHERE u.id = %s AND u.is_active AND v.state IN (%s, %s)",
                [user_id, STATE_VERIFIED, STATE_EXEMPT],
            )
            return cursor.fetchone() is not None
    except (TypeError, ValueError, OverflowError):
        return False
    except DatabaseError:
        logger.error("email verification eligibility lookup failed")
        return False


def is_pending(user_id):
    """True only for an existing PENDING account; failures return False."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM account_email_verification WHERE user_id = %s AND state = %s",
                [user_id, STATE_PENDING],
            )
            return cursor.fetchone() is not None
    except DatabaseError:
        logger.error("email verification state lookup failed")
        return False
