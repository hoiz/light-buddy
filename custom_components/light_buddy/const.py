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

# The one schedule for all rooms, a list of points.
CONF_SCHEDULE: Final = "schedule"
CONF_TIME: Final = "time"
CONF_KELVIN: Final = "kelvin"
CONF_WEEKDAYS: Final = "weekdays"
# Points at a sun event instead of a clock time, shifted, within a window.
CONF_ANCHOR: Final = "anchor"
CONF_SUN_OFFSET: Final = "offset"
CONF_EARLIEST: Final = "earliest"
CONF_LATEST: Final = "latest"
ANCHOR_TIME: Final = "time"

# Global settings.
CONF_SETTINGS: Final = "settings"
CONF_THRESHOLD: Final = "threshold"
CONF_TRANSITION: Final = "transition"
CONF_AUTO_RESUME_MINUTES: Final = "auto_resume_minutes"
CONF_SEPARATE_COMMANDS: Final = "separate_commands"
CONF_DISCO_INTERVAL: Final = "disco_interval"
CONF_DISCO_PALETTE: Final = "disco_palette"

# Which lights get brightness and color as separate commands.
SEPARATE_OFF: Final = "off"
SEPARATE_IKEA: Final = "ikea"
SEPARATE_ALL: Final = "all"

UNIT_KELVIN: Final = "K"
UNIT_MIRED: Final = "mired"

# Wait for registry changes to settle before recomputing room membership.
MEMBERSHIP_DEBOUNCE_SECONDS: Final = 1.0

# Minimum time between two commands of one room, e.g. while dragging a slider.
COMMAND_INTERVAL_SECONDS: Final = 0.3

# How often each room checks its schedule.
SCHEDULE_INTERVAL: Final = timedelta(seconds=60)

# Short transitions: IKEA lights ignore further commands, even turning off,
# while a transition runs. The schedule's is a setting, this its default.
TRANSITION_SCHEDULE: Final = 2
TRANSITION_TURN_ON: Final = 1

# Smaller changes of the target are not sent to lights that are on. A
# setting, this is its default.
COLOR_TEMP_THRESHOLD_KELVIN: Final = 50

# Changes outside Home Assistant count as manual from this difference to
# what was last sent, after the transition plus this grace.
MANUAL_COLOR_TEMP_THRESHOLD_KELVIN: Final = 150
TRANSITION_GRACE_SECONDS: Final = 2

# Between the color temperature and the brightness command of lights that
# get them separately, after the transition.
SEPARATE_COMMANDS_PAUSE_SECONDS: Final = 0.5

# Seconds between two colors of the disco mode, and its palette. Settings,
# these are their defaults. Each step sends one call per color.
DISCO_INTERVAL: Final = 4
DISCO_MIN_INTERVAL: Final = 2
DISCO_PALETTE: Final = "rainbow"
