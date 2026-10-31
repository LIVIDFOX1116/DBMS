"""Create a local admin account, or reset an existing account's password and
make it an active admin.

    python scripts/create_admin.py --username vishal --email you@example.com

The password is read with getpass -- typed twice, never echoed, never a
command-line argument (which would land in shell history and be visible to
every local process). Connects with the same settings as everything else
(PG* / .env). Needs migration 010 (roles and local accounts) applied.

This is how the first admin exists on a fresh deployment before any Google
sign-in is configured; after that, admins manage roles from the Users view.
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from api import auth  # noqa: E402
from db.orm.models import AppUser  # noqa: E402
from db.orm.session import get_session  # noqa: E402


def prompt_password(read=getpass.getpass) -> str:
    while True:
        first = read(f"New password (at least {auth.MIN_PASSWORD_LENGTH} characters): ")
        try:
            auth.validate_new_password(first)
        except ValueError as e:
            print(e)
            continue
        if read("Repeat it: ") != first:
            print("Passwords did not match; try again.")
            continue
        return first


def upsert_admin(session, username: str, email: str, password_hash: str) -> tuple[int, bool]:
    """(user_id, created). An existing username keeps its id -- the rows
    that reference it stay attached -- and becomes an active admin with the
    new password."""
    user = session.scalars(select(AppUser).where(AppUser.username == username)).one_or_none()
    created = user is None
    if created:
        user = AppUser(username=username, email=email)
        session.add(user)
    elif email:
        user.email = email
    user.password_hash = password_hash
    user.role = "admin"
    user.is_active = True
    session.flush()
    return user.user_id, created


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--username", required=True)
    parser.add_argument("--email", required=True)
    args = parser.parse_args(argv)
    if not (1 <= len(args.username) <= 40) or args.username.lower() == "system":
        print("username must be 1-40 characters and not 'system' (the service account)", file=sys.stderr)
        return 2
    password_hash = auth.hash_password(prompt_password())
    with get_session() as db:
        user_id, created = upsert_admin(db, args.username, args.email, password_hash)
    print(f"{'created' if created else 'updated'} admin '{args.username}' (user_id {user_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
