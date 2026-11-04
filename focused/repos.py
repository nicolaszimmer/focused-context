"""The repositories a session works in: the graph that recall and the choice of a session follow.

Decided by the maintainer on 2026-11-03: how sessions relate is a graph, "primarily the same
repo(s) the session uses or repos that had been together in one session before".

- **A session links to each repository it worked in**, weighted by its transcript entries:
  each entry's working directory (`cwd`), and each file or folder a tool was given, count
  once for the repository that holds it.
- **The repositories a session starts with** are its own too, at 0 entries until it works in
  them: its working directory's, or the git repositories directly in it, as a cloud session
  with several repositories has them.
- **Two repositories are related** when one session worked in both (the hosted index works
  this out over every session it holds; the choice of a session, over those it lists).

A repository is named by its remote `origin`, as `github.com/owner/name`, so it has one name
on every machine and in every container. A folder with no git remote has no such name, and
isn't counted.
"""

from __future__ import annotations

import os
import re
import subprocess
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

NAME = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+){1,4}")  # the hosted index takes these
SCP = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")  # git@github.com:owner/name.git
LONGEST = 200
LISTED = 50  # repositories sent with a recall


def name_for_url(url: str) -> Optional[str]:
    """`github.com/owner/name` for a remote's URL in any of git's forms (https, ssh, scp-like),
    without credentials, port or `.git`, in lower case (GitHub's names ignore case); None for
    a local path or anything else."""
    url = (url or "").strip()
    if "://" in url:
        try:
            parts = urllib.parse.urlsplit(url)
            host = parts.hostname or ""
        except ValueError:  # an unclosed IPv6 bracket, say
            return None
        if parts.scheme == "file":
            return None
        path = parts.path
    else:
        match = SCP.match(url)
        # A Windows path (C:/repos/name) is a local path, not a host and a path.
        if not match or url.startswith(("/", ".")) or len(match.group(1)) == 1:
            return None
        host, path = match.group(1).lower(), match.group(2)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    name = f"{host}/{path}".lower()
    return name if host and NAME.fullmatch(name) and len(name) <= LONGEST else None


def short(repo: str) -> str:
    """How a person names a repository: its last part."""
    return repo.rsplit("/", 1)[-1]


class Resolver:
    """Finds the repository holding a path: the nearest folder above it with a `.git`, named
    by its origin. Remembers each folder and each repository it has looked up."""

    def __init__(self):
        self._roots: dict = {}
        self._names: dict = {}

    def root(self, folder: Path) -> Optional[Path]:
        chain = []
        found = None
        for current in (folder, *folder.parents):
            if current in self._roots:
                found = self._roots[current]
                break
            chain.append(current)
            try:
                if (current / ".git").exists():
                    found = current
                    break
            except OSError:
                continue
        for seen in chain:
            self._roots[seen] = found
        return found

    def name(self, root: Path) -> Optional[str]:
        if root not in self._names:
            try:
                done = subprocess.run(["git", "-C", str(root), "config", "--get", "remote.origin.url"],
                                      capture_output=True, text=True, timeout=5,
                                      env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
                self._names[root] = name_for_url(done.stdout) if done.returncode == 0 else None
            except (OSError, subprocess.SubprocessError):
                self._names[root] = None
        return self._names[root]

    def repo(self, path: str) -> Optional[str]:
        """The repository holding `path`, a file or folder that may no longer exist."""
        if not path or not os.path.isabs(path):
            return None
        place = Path(os.path.normpath(path))
        folder = place if place.is_dir() else place.parent
        root = self.root(folder)
        return self.name(root) if root else None


def used(places: Counter, resolver: Optional[Resolver] = None) -> Counter:
    """Each repository's entries, from a transcript's places (transcript.Transcript.places)."""
    resolver = resolver or Resolver()
    counts: Counter = Counter()
    for path, entries in places.items():
        repo = resolver.repo(path)
        if repo:
            counts[repo] += entries
    return counts


def attached(cwd: Optional[str], resolver: Optional[Resolver] = None) -> list:
    """The repositories a session in `cwd` starts with: the one `cwd` is in, or else the git
    repositories directly in it, sorted."""
    if not cwd or not os.path.isdir(cwd):
        return []
    resolver = resolver or Resolver()
    own = resolver.repo(cwd)
    if own:
        return [own]
    found = set()
    try:
        children = sorted(os.scandir(cwd), key=lambda entry: entry.name)
    except OSError:
        return []
    for child in children:
        try:
            if child.is_dir() and not child.name.startswith(".") and os.path.exists(os.path.join(child.path, ".git")):
                repo = resolver.name(Path(child.path))
                if repo:
                    found.add(repo)
        except OSError:
            continue
    return sorted(found)


def of_session(places: Counter, start: Optional[str]) -> list:
    """A session's repositories as [(repo, entries)], most used first: those it worked in,
    and those it started with at 0."""
    resolver = Resolver()
    counts = used(places, resolver)
    for repo in attached(start, resolver):
        counts.setdefault(repo, 0)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def near(mine: Iterable[str], sessions: Iterable[Iterable[str]]) -> set:
    """The repositories worked on together with any of `mine` in one of `sessions` (each the
    repositories a session worked in), `mine` left out."""
    mine = set(mine)
    found: set = set()
    for repos in sessions:
        repos = set(repos)
        if repos & mine:
            found |= repos
    return found - mine


def relation(repos: Iterable[str], mine: set, related: set) -> Optional[str]:
    """"same" when a session worked in one of `mine`, "near" when in a related one, else None."""
    repos = set(repos)
    if repos & mine:
        return "same"
    if repos & related:
        return "near"
    return None


TIER = {"same": 2, "near": 1}


def describe(relation_: Optional[str], repos: Iterable[str], mine: Iterable[str]) -> str:
    """"same repository (x)" or "related repository (y)", or "" when unrelated."""
    repos, mine = list(repos), set(mine)
    if relation_ == "same":
        shared = [short(r) for r in repos if r in mine]
        return f"same repository ({', '.join(shared[:2])})" if shared else "same repository"
    if relation_ == "near":
        others = [short(r) for r in repos if r not in mine]
        return f"related repository ({', '.join(others[:2])})" if others else "related repository"
    return ""
