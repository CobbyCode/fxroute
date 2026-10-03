#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Install the built plugin into the stock SPA search path (requires privilege)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio.src_policy import install_plugin

if __name__ == "__main__":
    print(install_plugin())
