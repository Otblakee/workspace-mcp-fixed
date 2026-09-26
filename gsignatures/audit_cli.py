"""
Cron entry point: ``python -m gsignatures.audit_cli [scope] [--no-report]``.

Audits Gmail signatures against the ledger and exits:

* ``0`` when every row is ``in_sync`` or ``unmanaged``;
* ``2`` when any row is ``never_applied``, ``apply_interrupted``,
  ``stale_template``, ``stale_directory``, ``changed_since_apply`` or
  ``error`` (drift: Render records the failed run, and a human decides
  whether to re-apply);
* ``1`` on a fatal error (config, service-account auth, ledger, or a
  command line argparse rejects) with a one-line reason on stderr. A usage
  error is deliberately 1, not argparse's own 2, so a mistyped cron command
  can never be read as drift. ``--help`` still exits 0.

This process audits only. It never patches a signature: the only writes it
makes are the ``Ledger`` tab header on a Sheet that has none yet (the same
``prepare_ledger`` the audit tool uses, so both audit paths read one
contract) and the audit report tab (skip with ``--no-report``). There is no
MCP caller gate here because there is no MCP caller: the CLI is a trusted
process run by the platform with the same service-account key as the web
service.

Scopes are exactly one of ``--ou PATH``, ``--domain D``, ``--group G`` or
``--all`` (every active user in the tenant).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from typing import List, Optional

from core.utils import UserInputError

from gsignatures import operations
from gsignatures.engine import SignatureConfigError
from gsignatures.ledger import LedgerError, write_audit_report
from gsignatures.operations import build_runtime
from gsignatures.sa_auth import SignatureAuthError

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_DRIFT = 2

_FATAL = (SignatureAuthError, LedgerError, SignatureConfigError, UserInputError)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gsignatures.audit_cli",
        description=(
            "Audit Gmail signatures against the signature ledger. Exit 0 when "
            "everything managed is in sync, 2 on any drift, 1 on a fatal error."
        ),
    )
    parser.add_argument("--ou", metavar="PATH", help="an OU path, e.g. '/01 OTB'")
    parser.add_argument("--domain", metavar="D", help="a primary-address domain")
    parser.add_argument("--group", metavar="G", help="a group address (direct members)")
    parser.add_argument(
        "--all", action="store_true", help="every active user in the tenant"
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="do not write the Audit_<date> tab to the ledger Sheet",
    )
    parser.add_argument(
        "--tab-prefix",
        default="Audit",
        help="report tab prefix; the tab is <prefix>_<UTC date> (default: Audit)",
    )
    return parser


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


async def _run(args: argparse.Namespace) -> int:
    # Validate the scope before touching any client.
    label = operations.scope_label(
        ou_path=args.ou, domain=args.domain, group_email=args.group, all_users=args.all
    )
    runtime = build_runtime(need_ledger=True)
    # Same contract as the audit tool: tab ensured, header validated, every
    # failure a LedgerError. A Sheet with no Ledger tab yet is an empty
    # ledger, so a fresh deployment audits as never_applied (exit 2), not
    # as a fatal read error.
    ledger_latest = await operations.prepare_ledger(runtime.sheets, runtime.sheet_id)

    rows = await operations.audit_scope(
        runtime.config,
        runtime.directory,
        ou_path=args.ou,
        domain=args.domain,
        group_email=args.group,
        all_users=args.all,
        ledger_latest=ledger_latest,
        gmail_factory=runtime.gmail_factory,
    )

    print(f"Signature audit | scope {label} | rows {len(rows)}")
    print(operations.format_audit_counts(rows))
    print(operations.format_audit_table(rows))

    if not args.no_report:
        tab = f"{args.tab_prefix}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        try:
            await write_audit_report(runtime.sheets, runtime.sheet_id or "", tab, rows)
        except Exception as exc:
            raise LedgerError(
                f"the audit report tab {tab!r} could not be written "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        print(f"Report written to tab {tab}.")

    if operations.has_drift(rows):
        print("RESULT: drift detected (see the rows above). Exit 2.")
        return EXIT_DRIFT
    print("RESULT: no drift. Exit 0.")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on a usage error, which is this CLI's drift code.
        # Map it to fatal so a mistyped cron command is never read as drift;
        # --help (exit 0) stays 0.
        if not exc.code:
            return EXIT_OK
        print(
            "signature audit failed: the command line was rejected (see the "
            "usage message above); exit 1, not drift",
            file=sys.stderr,
        )
        return EXIT_FATAL
    try:
        return asyncio.run(_run(args))
    except _FATAL as exc:
        print(f"signature audit failed: {_one_line(str(exc))}", file=sys.stderr)
        return EXIT_FATAL
    except Exception as exc:  # anything else is still a fatal, one-line failure
        print(
            f"signature audit failed: {type(exc).__name__}: {_one_line(str(exc))}",
            file=sys.stderr,
        )
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
