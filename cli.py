import argparse
import logging
import sys

import uvicorn

from app.db.migrate import run_migrations
from app.main import configure_logging
from app.project.reproject import reproject


def cmd_serve(args: argparse.Namespace) -> int:
    configure_logging()
    logging.getLogger(__name__).info("starting personal-dashboard on 127.0.0.1:8080")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8080)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    configure_logging()
    applied = run_migrations()
    logger = logging.getLogger(__name__)
    if applied:
        logger.info("applied migrations: %s", applied)
    else:
        logger.info("no migrations to apply")
    return 0


def cmd_reproject(args: argparse.Namespace) -> int:
    configure_logging()
    logger = logging.getLogger(__name__)
    try:
        reproject(args.source)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    logger.info("reprojected source %r", args.source)
    return 0


def cmd_not_implemented(name: str):
    def _inner(args: argparse.Namespace) -> int:
        print(f"{name}: not implemented yet", file=sys.stderr)
        return 1

    return _inner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli.py")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve").set_defaults(func=cmd_serve)
    sub.add_parser("migrate").set_defaults(func=cmd_migrate)

    reproject_parser = sub.add_parser("reproject")
    reproject_parser.add_argument("source")
    reproject_parser.set_defaults(func=cmd_reproject)

    sub.add_parser("backup").set_defaults(func=cmd_not_implemented("backup"))

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
