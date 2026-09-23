#!/usr/bin/env python3
"""Launch the browser terminal: python run_web.py --port 8899 --auto"""

import sys

from cybertrade.cli import main

if __name__ == "__main__":
    sys.exit(main(["web", *sys.argv[1:]]))
