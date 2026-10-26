"""Constants for the Light Buddy integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "light_buddy"

CONF_EXCLUDED_AREAS: Final = "excluded_areas"
CONF_EXCLUDED_LIGHTS: Final = "excluded_lights"
# Offsets per entity registry id.
CONF_OFFSETS: Final = "offsets"
CONF_OFFSET_UNIT: Final = "unit"
CONF_OFFSET_VALUE: Final = "value"
# Areas whose schedule is turned off.
CONF_SCHEDULES_OFF: Final = "schedules_off"

UNIT_KELVIN: Final = "K"
UNIT_MIRED: Final = "mired"

# Wait for registry changes to settle before recomputing room membership.
MEMBERSHIP_DEBOUNCE_SECONDS: Final = 1.0

# Minimum time between two commands of one room, e.g. while dragging a slider.
COMMAND_INTERVAL_SECONDS: Final = 0.3

# How often each room checks its schedule.
SCHEDULE_INTERVAL: Final = timedelta(seconds=60)

# Short transitions: IKEA lights ignore further commands, even turning off,
# while a transition runs.
TRANSITION_SCHEDULE: Final = 2
TRANSITION_TURN_ON: Final = 1

# Smaller changes of the target are not sent to lights that are on.
COLOR_TEMP_THRESHOLD_KELVIN: Final = 50
BRIGHTNESS_THRESHOLD: Final = 5  # of 255, about 2 %

# Attributes the schedule leaves alone after they were changed by hand.
PAUSE_COLOR_TEMP: Final = "color_temp"
PAUSE_BRIGHTNESS: Final = "brightness"

# Changes outside Home Assistant count as manual from these differences to
# what was last sent, after the transition plus this grace.
MANUAL_COLOR_TEMP_THRESHOLD_KELVIN: Final = 150
MANUAL_BRIGHTNESS_THRESHOLD: Final = 13  # of 255, about 5 %
TRANSITION_GRACE_SECONDS: Final = 2

# Seconds after which a manual pause ends by itself, None to keep it until
# the room is off.
AUTO_RESUME: Final[float | None] = None
