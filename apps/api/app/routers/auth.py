from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DBSession

from app.audit import audit
from app.config import get_settings
from app.db import get_db
from app.deps import ApiError, AuthContext, current_auth
from app.models import Session, User, as_utc, utcnow
from app.schemas import LoginIn, UserOut
from app.security import hash_password, hash_token, needs_rehash, new_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def user_out(u: User) -> UserOut:
    return UserOut(id=u.id, name=u.name, email=u.email, status=u.status, role=u.role.name,
                   last_login_at=u.last_login_at, created_at=u.created_at)


def _set_cookies(response: Response, token: str, csrf: str, max_age: int) -> None:
    s = get_settings()
    response.set_cookie(s.session_cookie_name, token, max_age=max_age, httponly=True,
                        secure=s.cookie_secure, samesite="lax", path="/")
    # Readable by the frontend so it can echo it in X-CSRF-Token (double submit).
    response.set_cookie(s.csrf_cookie_name, csrf, max_age=max_age, httponly=False,
                        secure=s.cookie_secure, samesite="lax", path="/")


@router.post("/login", response_model=UserOut)
def login(body: LoginIn, request: Request, response: Response, db: DBSession = Depends(get_db)):
    s = get_settings()
    email = body.email.lower()
    user = db.scalar(select(User).where(User.email == email))
    now = utcnow()

    if user and user.locked_until and as_utc(user.locked_until) > now:
        audit(db, None, "auth.login_blocked", "user", user.id, reason="locked")
        db.commit()
        raise ApiError(429, "TOO_MANY_ATTEMPTS", "Too many failed attempts. Try again later.")

    ok = verify_password(body.password, user.password_hash if user else None)
    if not ok or user is None or user.status != "active":
        if user:
            user.failed_logins += 1
            if user.failed_logins >= s.max_failed_logins:
                user.locked_until = now + timedelta(minutes=s.lockout_minutes)
                user.failed_logins = 0
        audit(db, None, "auth.login_failed", "user", user.id if user else None,
              email=email, reason="inactive" if (ok and user) else "bad_credentials")
        db.commit()
        raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password.")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now

    token, csrf = new_token(), new_token()
    db.add(Session(
        user_id=user.id, token_hash=hash_token(token), csrf_token=csrf,
        expires_at=now + timedelta(minutes=s.session_idle_minutes),
        absolute_expires_at=now + timedelta(hours=s.session_absolute_hours),
        user_agent=(request.headers.get("user-agent") or "")[:300],
        ip_address=request.client.host if request.client else None,
    ))
    audit(db, user, "auth.login", "user", user.id)
    db.commit()
    _set_cookies(response, token, csrf, s.session_idle_minutes * 60)
    return user_out(user)


@router.post("/logout", status_code=204)
def logout(response: Response, auth: AuthContext = Depends(current_auth),
           db: DBSession = Depends(get_db)):
    auth.session.revoked_at = utcnow()
    audit(db, auth.user, "auth.logout", "user", auth.user.id)
    db.commit()
    s = get_settings()
    response.delete_cookie(s.session_cookie_name, path="/")
    response.delete_cookie(s.csrf_cookie_name, path="/")


@router.post("/refresh", response_model=UserOut)
def refresh(response: Response, auth: AuthContext = Depends(current_auth),
            db: DBSession = Depends(get_db)):
    """Rotate the session token and CSRF token and extend the idle window."""
    s = get_settings()
    now = utcnow()
    token, csrf = new_token(), new_token()
    sess = auth.session
    sess.token_hash = hash_token(token)
    sess.csrf_token = csrf
    sess.last_seen_at = now
    sess.expires_at = min(now + timedelta(minutes=s.session_idle_minutes),
                          as_utc(sess.absolute_expires_at))
    db.commit()
    _set_cookies(response, token, csrf, s.session_idle_minutes * 60)
    return user_out(auth.user)


@router.get("/me", response_model=UserOut)
def me(auth: AuthContext = Depends(current_auth)):
    return user_out(auth.user)


def revoke_user_sessions(db: DBSession, user_id) -> None:
    db.execute(update(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None))
               .values(revoked_at=utcnow()))
