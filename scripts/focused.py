#!/usr/bin/env python3
"""Run the focused command from a checkout or an installed plugin, without installing it.

The plugin's hook and skills call this file, so the plugin needs nothing but Python 3.10
or later: the focused package next to it uses the standard library only.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from focused.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
