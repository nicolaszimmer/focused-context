"""Keep a cloud environment's focused up to date with a branch of focused-context.

A cloud environment runs its setup script once and reuses the result for about seven
days, running the script again only when the script itself changes. So a focused
installed by the setup script would stay at the version it fetched. With
`install --follow <branch>`, focused also installs a SessionStart hook that runs
`update()` at the start of each session: when the branch has moved, it fetches that
commit and runs that version's installer, which takes over the hooks and commands.
Decided by the maintainer on 2026-11-03: cloud sessions follow focused-context's main,
so a merge to main is a release.

Everything fails open: no network, no git, a bad commit, and the session goes on with
the focused it has; the hook writes why to the log.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

REPOSITORY = "https://github.com/nicolaszimmer/focused-context"
REF_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")  # it goes into a shell command
COMMIT = re.compile(r"[0-9a-f]{40}")


class UpdateError(Exception):
    pass


def repository() -> str:
    return os.environ.get("FOCUSED_REPOSITORY", REPOSITORY)  # another clone, for tests


def cache_dir() -> Path:
    return Path(os.environ.get("FOCUSED_CACHE", "~/.cache/focused-context")).expanduser()


def _git(*args, timeout: float = 30, cwd: Optional[Path] = None) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"git {args[0]}: {error}") from None
    if done.returncode != 0:
        raise UpdateError(f"git {args[0]}: {(done.stderr or done.stdout).strip()[:300]}")
    return done.stdout


def latest(ref: str, timeout: float = 15) -> str:
    """The commit a branch (or tag) of the repository is at now."""
    if not REF_NAME.fullmatch(ref):
        raise UpdateError(f"{ref!r} isn't a plain branch name")
    found = {}
    for line in _git("ls-remote", repository(), f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}",
                     timeout=timeout).splitlines():
        commit, _, name = line.partition("\t")
        if COMMIT.fullmatch(commit):
            found[name] = commit
    for name in (f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"):
        if name in found:
            return found[name]
    raise UpdateError(f"{repository()} has no branch or tag {ref}")


def current(root: Path) -> Optional[str]:
    """The commit a focused-context checkout is at, if it is a git checkout."""
    try:
        commit = _git("rev-parse", "HEAD", cwd=root, timeout=10).strip()
    except UpdateError:
        return None
    return commit if COMMIT.fullmatch(commit) else None


def fetch(commit: str) -> Path:
    """A checkout of the commit in the cache, fetched once, as the setup script does."""
    target = cache_dir() / commit
    if (target / "scripts" / "focused.py").exists():
        return target
    temporary = cache_dir() / f".{commit}.{os.getpid()}"
    temporary.mkdir(parents=True, exist_ok=True)
    try:
        _git("init", "-q", str(temporary))
        _git("-C", str(temporary), "fetch", "-q", "--depth", "1", repository(), commit, timeout=120)
        _git("-C", str(temporary), "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD")
        if not (temporary / "scripts" / "focused.py").exists():
            raise UpdateError(f"{commit} isn't a focused-context commit")
        try:
            temporary.rename(target)
        except OSError:  # another session got there first
            if not (target / "scripts" / "focused.py").exists():
                raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)  # whatever didn't become the checkout
    return target


def update(root: Path, ref: str, jev: bool) -> str:
    """Bring the install to where `ref` is now, with that version's own installer, which
    replaces the hooks and commands this one put in place. Returns what happened."""
    commit = latest(ref)
    if commit == current(root):
        return f"focused is at {ref} ({commit[:8]})"
    new = fetch(commit)
    command = [sys.executable, str(new / "scripts" / "focused.py"), "install", "--root", str(new), "--follow", ref]
    done = subprocess.run(command + (["--jev"] if jev else []), capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise UpdateError(f"the installer of {commit[:8]} failed: {(done.stderr or done.stdout).strip()[:300]}")
    return f"focused updated to {ref} ({commit[:8]}), from {(current(root) or 'an unknown commit')[:8]}"
