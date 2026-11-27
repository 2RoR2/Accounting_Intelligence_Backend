"""Add the Fraud Shield and AI Inbox demo cases to the KNT demo company.

    .venv\\Scripts\\python scripts\\seed_ai_demo_cases.py            # dry run: prints what it would add
    .venv\\Scripts\\python scripts\\seed_ai_demo_cases.py --apply    # writes to the local database

UAT (a hosted database write) needs Jonathan's approval first. Then set AI_DEMO_REMOTE_HOST to the database host name
in the environment and pass --allow-remote --apply. The script never prints the database URL or any secret.
See services/insights/demo_cases.py for the cases.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description='Add the Fraud Shield and AI Inbox demo cases to KNT.')
    parser.add_argument('--apply', action='store_true', help='write the cases; without it only the plan is printed')
    parser.add_argument('--allow-remote', action='store_true',
                        help='permit a database that is not on this machine (also needs AI_DEMO_REMOTE_HOST)')
    args = parser.parse_args()
    from services.insights import demo_cases

    try:
        connect = demo_cases.require_target(args.allow_remote)
        demo_cases.seed(connect, apply=args.apply)
    except demo_cases.DemoError as error:
        parser.exit(2, f'{error}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
