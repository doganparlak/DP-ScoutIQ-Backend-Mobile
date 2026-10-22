"""Explicit account setup and one-shot execution; periodic runs use the worker.

Run: python -m scripts.community_automation --apply
Use --disable to stop future submissions without deleting existing entries.
"""
import argparse
from sqlalchemy import text
from api_module.database import SessionLocal
from community_automation_module.automation import seed_accounts, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--apply', action='store_true')
    group.add_argument('--disable', action='store_true')
    args = parser.parse_args()
    with SessionLocal.begin() as db:
        if args.disable:
            db.execute(text('UPDATE community_automation_settings SET enabled=FALSE WHERE id=TRUE'))
            print('Future automation disabled; existing entries retained.')
        else:
            seed_accounts(db)
            db.execute(text('UPDATE community_automation_settings SET enabled=TRUE WHERE id=TRUE'))
            print('Account registry ready: 10 Pro, 18 Plus, 22 Free.')
    if args.apply:
        with SessionLocal.begin() as db:
            print('Submitted:', run(db))


if __name__ == '__main__':
    main()
