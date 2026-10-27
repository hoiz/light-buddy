"""Render the example curve of the README.

.venv/bin/python assets/render_curve.py
"""

import datetime as dt
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.light_buddy.chart import render_svg
from custom_components.light_buddy.schedule import DEFAULT_SCHEDULE, Sun

# Berlin on an autumn afternoon.
NOW = dt.datetime(2026, 10, 27, 15, 0, tzinfo=ZoneInfo("Europe/Berlin"))
SUN = Sun(52.52, 13.40)

Path(__file__).with_name("curve.svg").write_text(
    render_svg(DEFAULT_SCHEDULE, NOW, "en", SUN)
)
