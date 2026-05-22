"""
paths.py — shared helpers for ensuring project directories exist.

The migration tool reads/writes three top-level folders:
  - config/   .env files (source + target credentials)
  - exports/  raw JSON snapshots from the source account
  - state/    id_map.json + migration_log.jsonl

If a folder is missing — typically on a fresh checkout, or after the user
moves files around — we used to create it silently. That hides accidents
(e.g. running from the wrong working directory creates an empty state/ in
the wrong place). `ensure_dirs` instead prompts the user for confirmation
before each missing directory is created.

The prompt is skipped (auto-create) when:
  - stdin is not a TTY (CI, piped input)
  - `assume_yes=True` is passed (for `--yes` flag callers)
"""

import sys
from pathlib import Path
from typing import Iterable, Optional


def _is_interactive() -> bool:
    """True if both stdin and stdout are attached to a TTY."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    """
    Minimal y/n prompt with a default. Defaults to yes (creating the folder)
    because the folder is required for the tool to function.
    """
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in ("y", "yes")


def ensure_dirs(
    dirs: Iterable[Path],
    *,
    assume_yes: bool = False,
    project_root: Optional[Path] = None,
) -> bool:
    """
    Confirm-and-create each directory in `dirs` that does not exist.

    Returns True if every directory now exists (either pre-existing or
    just created with consent). Returns False if the user declined any
    creation — caller should abort.

    `project_root` is used only to render shorter relative paths in the
    prompt. If a directory cannot be made relative to it, the absolute
    path is shown instead.
    """
    interactive = _is_interactive() and not assume_yes

    for d in dirs:
        d = Path(d)
        if d.exists():
            if not d.is_dir():
                print(
                    f"✗  '{d}' exists but is not a directory. "
                    "Move it aside and re-run."
                )
                return False
            continue

        display = d
        if project_root:
            try:
                display = d.relative_to(project_root)
            except ValueError:
                pass

        if interactive:
            ok = _ask_yes_no(
                f"Directory '{display}/' does not exist. Create it now?",
                default=True,
            )
            if not ok:
                print(f"✗  Cannot continue without '{display}/'.")
                return False

        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"✗  Could not create '{display}/': {exc}")
            return False

        print(f"  Created '{display}/'")

    return True
