"""``hermes vibecop`` subcommand parser.

Fleet-specific (see ``docs/fleet/PATCHES.md``): manages the vibecop Guardian
mode — an editable, project-aware prompt for the smart-approval LLM. Distinct
from upstream's own ``hermes approvals`` tools, which are a different design.
"""

from __future__ import annotations

from typing import Callable


def build_vibecop_parser(subparsers, *, cmd_vibecop: Callable) -> None:
    """Attach the ``vibecop`` subcommand to ``subparsers``."""
    vibecop_parser = subparsers.add_parser(
        "vibecop",
        help="Project-aware smart-approval prompt (vibecop Guardian mode)",
        description="Manage vibecop Guardian mode: an editable, project-specific "
            "prompt for the smart-approval LLM, with a workspace snapshot and "
            "recent verdict history injected into each assessment.")
    vibecop_subparsers = vibecop_parser.add_subparsers(
        dest="vibecop_command", metavar="<subcommand>")

    status_parser = vibecop_subparsers.add_parser(
        "status", help="Show vibecop config and prompt status")
    status_parser.set_defaults(func=cmd_vibecop)

    init_parser = vibecop_subparsers.add_parser(
        "init", help="Generate a project-specific vibecop prompt")
    init_parser.set_defaults(func=cmd_vibecop)

    refine_parser = vibecop_subparsers.add_parser(
        "refine", help="Regenerate the vibecop prompt using recent activity")
    refine_parser.set_defaults(func=cmd_vibecop)

    vibecop_parser.set_defaults(func=cmd_vibecop)
