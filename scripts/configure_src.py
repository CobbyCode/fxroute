#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Install common SRC adapter rules without restarting services."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio.src_policy import activate_environment, configure

if __name__ == "__main__":
    for path in configure():
        print(path)
    activate_environment()
