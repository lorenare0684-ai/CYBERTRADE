#!/usr/bin/env python3
"""Launch the desktop terminal: python run_gui.py"""

import sys

from cybertrade.cli import main

if __name__ == "__main__":
    sys.exit(main(["gui", *sys.argv[1:]]))
