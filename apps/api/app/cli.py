"""Bootstrap commands:  python -m app.cli seed-roles | create-admin --email ... [--name ...]

The admin password is read from NEXORA_ADMIN_PASSWORD or prompted (never a CLI argument).
"""

import argparse
import getpass
import os
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_engine
from app.models import DEVELOPER, ROLE_NAMES, SUPER_ADMIN, TEAM_ADMIN, Role, User
from app.security import MIN_PASSWORD_LENGTH, hash_password

ROLE_PERMISSIONS = {
    SUPER_ADMIN: {"scope": "platform"},
    TEAM_ADMIN: {"scope": "team"},
    DEVELOPER: {"scope": "project"},
}


def seed_roles(db: Session) -> None:
    existing = {r.name for r in db.scalars(select(Role))}
    for name in ROLE_NAMES:
        if name not in existing:
            db.add(Role(name=name, permissions=ROLE_PERMISSIONS[name]))
    db.commit()


def main() -> int:
    p = argparse.ArgumentParser(prog="nexora")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed-roles")
    a = sub.add_parser("create-admin")
    a.add_argument("--email", required=True)
    a.add_argument("--name", default="Administrator")
    args = p.parse_args()
    with Session(get_engine()) as db:
        seed_roles(db)
        if args.cmd == "create-admin":
            pw = os.environ.get("NEXORA_ADMIN_PASSWORD") or getpass.getpass("Password: ")
            if len(pw) < MIN_PASSWORD_LENGTH:
                print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
                return 1
            email = args.email.lower()
            if db.scalar(select(User).where(User.email == email)):
                print("User already exists.", file=sys.stderr)
                return 1
            role = db.scalar(select(Role).where(Role.name == SUPER_ADMIN))
            user = User(name=args.name, email=email, role_id=role.id,
                        password_hash=hash_password(pw))
            db.add(user)
            db.flush()
            audit(db, None, "user.bootstrap_admin", "user", user.id)
            db.commit()
            print(f"Created super admin {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
