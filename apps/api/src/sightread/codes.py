"""`python -m sightread.codes`: the operator's redeem codes — `create`, `list`, `disable`
(docs/self-hosting.md § Redeem codes, docs/billing.md § Redeem codes). Runs in the api
container against the deployment's database; there is no page for it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .billing import codes
from .config import Settings, get_settings
from .db.models import RedeemCode
from .db.session import create_engine, create_sessionmaker


def _redeem_by(value: str) -> datetime:
    """`YYYY-MM-DD`: the last UTC day the code can be redeemed on."""
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a date as YYYY-MM-DD") from None
    return datetime.combine(day, datetime.max.time().replace(microsecond=0), UTC)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected an integer >= 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m sightread.codes", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="print fresh codes, one per line")
    grant = create.add_mutually_exclusive_group(required=True)
    grant.add_argument("--credits", type=_positive, help="credits each code gives")
    grant.add_argument("--plan", help="the plan (of KANO_PAY_PLANS_JSON) each code gives days of")
    create.add_argument(
        "--valid-days", type=_positive, default=365, help="how long the credits last (365)"
    )
    create.add_argument("--days", type=_positive, help="days of the plan each code gives")
    create.add_argument("--count", type=_positive, default=1, help="how many codes (1)")
    create.add_argument(
        "--uses", type=_positive, default=1, help="how many accounts may redeem one code (1)"
    )
    create.add_argument(
        "--redeem-by", type=_redeem_by, help="the last day (UTC, YYYY-MM-DD) a code can be redeemed"
    )
    create.add_argument("--label", default="", help="your note; a user never sees it")

    listing = commands.add_parser("list", help="the live codes and their uses")
    listing.add_argument(
        "--all", action="store_true", help="disabled, exhausted and expired codes too"
    )

    disable = commands.add_parser("disable", help="a code is dead from now on")
    disable.add_argument("code", help="the code as printed")
    return parser


def _grant(row: RedeemCode) -> str:
    if row.kind == codes.CREDITS:
        return f"{row.credits} credits, {row.valid_days} days"
    return f"{row.days} days of {row.plan_id}"


def _state(row: RedeemCode, now: datetime) -> str:
    if row.disabled_at is not None:
        return "disabled"
    if row.redeem_by is not None and now > row.redeem_by:
        return "expired"
    if row.uses >= row.max_uses:
        return "used up"
    return "live"


async def run(args: argparse.Namespace, settings: Settings, db: AsyncSession) -> int:
    prefix = settings.redeem_code_prefix
    if args.command == "create":
        if args.plan is not None and args.days is None:
            print("--plan needs --days", file=sys.stderr)
            return 2
        try:
            rows = await codes.create_codes(
                db,
                settings,
                kind=codes.CREDITS if args.credits is not None else codes.PLAN,
                count=args.count,
                max_uses=args.uses,
                redeem_by=args.redeem_by,
                label=args.label,
                credits=args.credits,
                valid_days=args.valid_days,
                plan_id=args.plan,
                days=args.days,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        for row in rows:
            print(codes.display(row.code, prefix))
        return 0
    if args.command == "list":
        now = datetime.now(UTC)
        rows = (await db.execute(select(RedeemCode).order_by(RedeemCode.id))).scalars()
        for row in rows:
            state = _state(row, now)
            if state != "live" and not args.all:
                continue
            until = row.redeem_by.date().isoformat() if row.redeem_by else "-"
            print(
                f"{codes.display(row.code, prefix)}\t{_grant(row)}\t{row.uses}/{row.max_uses}"
                f"\t{until}\t{state}\t{row.label}"
            )
        return 0
    row = await codes.disable(db, settings, args.code)
    if row is None:
        print("no such code", file=sys.stderr)
        return 1
    print(f"disabled {codes.display(row.code, prefix)}")
    return 0


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    engine = create_engine(settings.database_url)
    sessionmaker: async_sessionmaker[AsyncSession] = create_sessionmaker(engine)
    try:
        async with sessionmaker() as db:
            return await run(args, settings, db)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
