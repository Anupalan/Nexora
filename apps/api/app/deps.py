import hmac
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.config import get_settings
from app.db import get_db
from app.models import SUPER_ADMIN, Session, User, as_utc, utcnow
from app.security import hash_token

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class ApiError(HTTPException):
    """Raises the standard Nexora error envelope (roadmap §11)."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(status_code=status, detail={"code": code, "message": message,
                                                      "details": details or {}})


@dataclass
class AuthContext:
    user: User
    session: Session


def current_auth(request: Request, db: DBSession = Depends(get_db)) -> AuthContext:
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise ApiError(401, "UNAUTHENTICATED", "Authentication required.")
    sess = db.scalar(select(Session).where(Session.token_hash == hash_token(token)))
    now = utcnow()
    if (
        sess is None
        or sess.revoked_at is not None
        or as_utc(sess.expires_at) <= now
        or as_utc(sess.absolute_expires_at) <= now
        or sess.user.status != "active"
    ):
        raise ApiError(401, "UNAUTHENTICATED", "Session is invalid or expired.")

    if request.method in UNSAFE:
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, sess.csrf_token):
            raise ApiError(403, "CSRF_FAILED", "Missing or invalid CSRF token.")

    # Sliding idle expiry, bounded by the absolute lifetime; write at most once a minute.
    if (now - as_utc(sess.last_seen_at)) > timedelta(seconds=60):
        sess.last_seen_at = now
        sess.expires_at = min(
            now + timedelta(minutes=settings.session_idle_minutes),
            as_utc(sess.absolute_expires_at),
        )
        db.commit()
    return AuthContext(user=sess.user, session=sess)


def current_user(auth: AuthContext = Depends(current_auth)) -> User:
    return auth.user


def require_super_admin(user: User = Depends(current_user)) -> User:
    if user.role.name != SUPER_ADMIN:
        raise ApiError(403, "FORBIDDEN", "Super admin access required.")
    return user
