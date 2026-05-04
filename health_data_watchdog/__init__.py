"""Health Data Watchdog — public health dataset surveillance CLI tool.

This package provides utilities for periodically fetching public health
surveillance datasets from CDC, WHO, and other sources, diffing them
against cached snapshots, and alerting researchers to changes.
"""

from __future__ import annotations

__version__ = "0.1.0"
__author__ = "Health Data Watchdog Contributors"
__license__ = "MIT"

# Top-level symbols that consumers of the package may import directly.
__all__ = [
    "__version__",
    "__author__",
    "__license__",
]
