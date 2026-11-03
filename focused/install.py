"""Install focused for every session of a Claude Code cloud environment.

A cloud session runs a repository's hooks only when it has that one repository. An
environment's setup script, though, runs before Claude Code starts in every session of
the environment, and Claude Code reads hooks and skills from the container's own
`~/.claude`. Checked on 2026-11-03: hooks merged into `~/.claude/settings.json` by the
setup script ran in a session with two repositories.

So the setup script fetches focused at a pinned commit and runs `scripts/install-cloud.sh`,
which calls `install()`:

- **Hooks**, merged into `~/.claude/settings.json`: Stop (async: indexing, and Jev with
  `jev=True`), SessionEnd, SessionStart after /clear and compaction, and UserPromptSubmit
  (the stop after an hour without a reply).
- **Commands**, as personal skills `focused-answers`, `focused-find` and `focused-context`
  in `~/.claude/skills`. A personal skill takes precedence over a repository's skill of
  the same name, so a repository's own copies are shadowed, not doubled.
- **`FOCUSED_INSTALL=environment`** in the settings' `env`, so a repository's own focused
  hooks (rapid-review-bot's wrapper) stand down instead of running a second time.

Running it again replaces what it installed, and `uninstall()` removes it. Other hooks,
skills and settings are left as they are: focused's hooks are recognised by MARK.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

MARK = "# focused-install"
SKILLS = ("answers", "find", "context")
SKILL_MARK = ".focused-install"
ENV_MARK = ("FOCUSED_INSTALL", "environment")
# The checkout path goes into shell commands and skills between double quotes, so it may
# hold only characters that mean nothing to the shell there: no $, `, \ or ", no spaces.
SAFE_PATH = re.compile(r"[A-Za-z0-9_./+@%=:,-]+")


def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()


def package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _hooks(root: Path, jev: bool) -> dict:
    script = f'python3 "{root}/scripts/focused.py" hook'

    def command(event: str, timeout: Optional[int] = None, prefix: str = "", run_async: bool = False) -> dict:
        hook = {"type": "command", "command": f"{prefix}{script} {event}  {MARK}"}
        if timeout:
            hook["timeout"] = timeout
        if run_async:
            hook["async"] = True
        return hook

    return {
        "Stop": [{"hooks": [command("stop", prefix="FOCUSED_JEV=1 " if jev else "", run_async=True)]}],
        "SessionEnd": [{"hooks": [command("session-end", timeout=15)]}],
        "SessionStart": [{"matcher": "clear|compact", "hooks": [command("session-start", timeout=30)]}],
        "UserPromptSubmit": [{"hooks": [command("prompt", timeout=15)]}],
    }


def _ours(hook) -> bool:
    return isinstance(hook, dict) and MARK in str(hook.get("command", ""))


def _without_ours(hooks: dict) -> dict:
    """The hooks settings with every hook focused installed taken out, and any group or
    event left empty by that removed."""
    out = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            out[event] = groups
            continue
        kept = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                remaining = [h for h in group["hooks"] if not _ours(h)]
                if not remaining:
                    continue
                group = dict(group, hooks=remaining)
            kept.append(group)
        if kept:
            out[event] = kept
    return out


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError as error:
        raise ValueError(f"{path} isn't valid JSON, so it was left as it is: {error}") from None
    if not isinstance(settings, dict):
        raise ValueError(f"{path} doesn't hold a JSON object, so it was left as it is")
    for key in ("hooks", "env"):
        if settings.get(key) is not None and not isinstance(settings[key], dict):
            raise ValueError(f"{path} has a \"{key}\" value that isn't an object, so it was left as it is")
    for event, groups in (settings.get("hooks") or {}).items():
        if not isinstance(groups, list):
            raise ValueError(f"{path} has \"{event}\" hooks that aren't a list, so it was left as it is")
    return settings


def _write(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    temporary = path.with_name(f".{path.name}.focused-{os.getpid()}")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w", encoding="utf-8") as handle:
        handle.write(json.dumps(settings, indent=2) + "\n")
    os.replace(temporary, path)


def _skill(root: Path, name: str) -> str:
    text = (root / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"skills/{name}/SKILL.md has no front matter")
    text = text.replace("${CLAUDE_PLUGIN_ROOT}", str(root))
    return f"---\nname: focused-{name}\n" + text[len("---\n"):]


def install(root: Optional[Path] = None, jev: bool = False, settings_path: Optional[Path] = None,
            skills_dir: Optional[Path] = None) -> list:
    """Install the hooks, skills and mark; returns what was done, line by line."""
    root = Path(root or package_root()).resolve()
    if not SAFE_PATH.fullmatch(str(root)):
        raise ValueError(f"{root} holds characters the shell would read, so no hooks point at it")
    if not (root / "scripts" / "focused.py").exists():
        raise ValueError(f"{root} isn't a focused-context checkout this can point hooks at")
    settings_path = settings_path or config_dir() / "settings.json"
    skills_dir = skills_dir or config_dir() / "skills"

    # Everything that can refuse is checked before anything is written.
    settings = _read(settings_path)
    for name in SKILLS:
        folder = skills_dir / f"focused-{name}"
        if folder.exists() and not (folder / SKILL_MARK).exists():
            raise ValueError(f"{folder} exists and isn't focused's, so nothing was installed")
    skills = {name: _skill(root, name) for name in SKILLS}

    hooks = _without_ours(settings.get("hooks") or {})
    for event, groups in _hooks(root, jev).items():
        hooks[event] = hooks.get(event, []) + groups
    settings["hooks"] = hooks
    settings["env"] = dict(settings.get("env") or {}, **{ENV_MARK[0]: ENV_MARK[1]})
    _write(settings_path, settings)

    for name, text in skills.items():
        folder = skills_dir / f"focused-{name}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(text, encoding="utf-8")
        (folder / SKILL_MARK).write_text(str(root) + "\n", encoding="utf-8")
    return [f"hooks in {settings_path}: Stop{' with Jev' if jev else ''}, SessionEnd, "
            "SessionStart (clear|compact), UserPromptSubmit",
            f"commands in {skills_dir}: " + ", ".join(f"/focused-{name}" for name in SKILLS),
            f"from {root}"]


def uninstall(settings_path: Optional[Path] = None, skills_dir: Optional[Path] = None) -> list:
    """Remove what install() put in place, and nothing else."""
    settings_path = settings_path or config_dir() / "settings.json"
    skills_dir = skills_dir or config_dir() / "skills"
    done = []
    if settings_path.exists():
        settings = _read(settings_path)
        hooks = _without_ours(settings.get("hooks") or {})
        if hooks:
            settings["hooks"] = hooks
        else:
            settings.pop("hooks", None)
        env = dict(settings.get("env") or {})
        if env.get(ENV_MARK[0]) == ENV_MARK[1]:
            env.pop(ENV_MARK[0])
        if env:
            settings["env"] = env
        else:
            settings.pop("env", None)
        _write(settings_path, settings)
        done.append(f"hooks removed from {settings_path}")
    for name in SKILLS:
        folder = skills_dir / f"focused-{name}"
        if (folder / SKILL_MARK).exists():
            shutil.rmtree(folder)
            done.append(f"removed /focused-{name}")
    return done
