"""
tests/conftest.py — Shared pytest fixtures and configuration.

Ensures the gateway package root is on sys.path so imports work correctly
when running 'pytest tests/' from the gateway/ directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add the gateway/ directory to sys.path so all gateway modules are importable
gateway_root = Path(__file__).parent.parent
if str(gateway_root) not in sys.path:
    sys.path.insert(0, str(gateway_root))
