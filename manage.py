"""Administrative commands.

Create an additional organization with its owner:
    python manage.py create-owner --org "Example Org" --username admin

If --password is omitted, a random password is generated and printed once.
The first owner is normally created at startup from INITIAL_* variables instead.
"""
import argparse
import secrets
import sys

import psycopg

from app import auth, db


def create_owner(args: argparse.Namespace) -> None:
    password = args.password or secrets.token_urlsafe(12)
    if len(password) < 8:
        sys.exit("Password must be at least 8 characters long")
    db.init_db()
    try:
        db.create_organization_with_owner(args.org, args.username, auth.hash_password(password))
    except psycopg.errors.UniqueViolation:
        sys.exit("Username is already taken")
    print(f"Organization: {args.org}")
    print(f"Username: {args.username}")
    if not args.password:
        print(f"Password: {password}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    owner = sub.add_parser("create-owner", help="create an organization and its owner")
    owner.add_argument("--org", required=True, help="organization name")
    owner.add_argument("--username", required=True)
    owner.add_argument("--password", help="omit to generate one (avoids shell history)")
    owner.set_defaults(func=create_owner)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
