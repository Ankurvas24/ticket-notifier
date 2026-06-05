#!/usr/bin/env python3
"""
Entry point for the standalone Selenium stealth client.

    cd selenium-client
    pip install -r requirements.txt
    python run.py

Configuration is via environment variables — see README.md and stealth_client/config.py.
"""

from stealth_client.runner import run

if __name__ == "__main__":
    run()
