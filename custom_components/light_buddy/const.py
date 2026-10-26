"""Constants for the Light Buddy integration."""

from typing import Final

DOMAIN: Final = "light_buddy"

CONF_EXCLUDED_AREAS: Final = "excluded_areas"
CONF_EXCLUDED_LIGHTS: Final = "excluded_lights"
# Offsets in Kelvin per entity registry id.
CONF_OFFSETS: Final = "offsets"

# Wait for registry changes to settle before recomputing room membership.
MEMBERSHIP_DEBOUNCE_SECONDS: Final = 1.0

# Minimum time between two commands of one room, e.g. while dragging a slider.
COMMAND_INTERVAL_SECONDS: Final = 0.3
