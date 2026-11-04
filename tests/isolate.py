"""Imported first by every test module: the tests never reach the services the machine
they run on is set up for. A cloud environment for focused has FOCUSED_REMOTE_URL and its
token (the maintainer's hosted index) and an OpenRouter key (Jev) in its environment;
hooks the tests run, in this process or in one it starts, would otherwise push their
sessions there. Each test sets what it needs."""

import os

for name in list(os.environ):
    if name.startswith("FOCUSED_") or name == "OPENROUTER_API_KEY":
        del os.environ[name]
