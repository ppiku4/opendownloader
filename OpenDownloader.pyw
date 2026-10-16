"""Double-click launcher (no console window on Windows)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from opendownloader.__main__ import main  # noqa: E402

sys.exit(main())
