"""``python -m newspulse.web`` — start the dashboard server.

A thin wrapper over ``app.main`` so the dashboard can be launched either via the
``newspulse-web`` console script or as a module.
"""

from __future__ import annotations

from .app import main

if __name__ == "__main__":
    main()
