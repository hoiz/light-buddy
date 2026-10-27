<p align="center">
  <img src="custom_components/light_buddy/brand/icon@2x.png" alt="Light Buddy" width="128">
</p>

# Light Buddy

[![Validate](https://github.com/hoiz/light-buddy/actions/workflows/validate.yml/badge.svg)](https://github.com/hoiz/light-buddy/actions/workflows/validate.yml)
[![Tests](https://github.com/hoiz/light-buddy/actions/workflows/tests.yml/badge.svg)](https://github.com/hoiz/light-buddy/actions/workflows/tests.yml)

A custom integration for Home Assistant: **one light per room**, without maintaining light groups.

For every area with at least one light, Light Buddy creates a light entity, e.g. `light.all_living` named "All lights in Living". It switches and dims all lights of the area together and works in a regular dashboard tile with a brightness slider.

On top of that, there is a color temperature offset per light, a color temperature schedule and a disco mode.

## Requirements

Home Assistant 2026.10 or newer.

## Installation

**HACS:** HACS → Integrations → menu → Custom repositories → add the URL of this repository with the type "Integration". Then install "Light Buddy" and restart Home Assistant.

**Manually:** Copy the folder `custom_components/light_buddy` into the `custom_components` folder of your Home Assistant configuration and restart Home Assistant.

## Setup

Settings → Devices & services → Add integration → **Light Buddy**. There is nothing else to set up; the rooms are created automatically.

## Which lights belong to a room?

- Lights whose entity is assigned to the area.
- Lights without an area of their own whose device is in the area.
- Not included: disabled entities, light groups (`group`) and Light Buddy's own entities.

When you move a light, or add or rename an area, Light Buddy follows without a restart. Areas without lights lose their room light.

## Exclusions

Settings → Devices & services → Light Buddy → **Configure** → **Exclusions**:

- **Excluded areas**: these areas get no room light.
- **Excluded lights**: these lights belong to no room, e.g. template lights that control other lights themselves.

## Color temperature offset

For a light that looks colder or warmer than the others. Every light with color temperature in a room has two settings on its device page, under **Configuration**:

- **Color temperature offset**: a slider for the offset.
- **Color temperature offset unit**: Kelvin or mired. Switching converts the offset so the light looks the same at 3000 K.

**Kelvin or mired?** A Kelvin offset is strong in warm light and barely noticeable in cold light: -300 K is clearly visible at 2700 K and almost invisible at 6000 K. A mired offset looks equally strong everywhere, matching how the eye perceives color temperature. In Kelvin, a negative value makes the light warmer; in mired, a positive one does.

**Try it out:** Turn the room on. Every change of the slider applies to the light right away.

1. Set the room to a warm value, e.g. 2700 K, and adjust the offset until the light matches the others.
2. Set the room to a cold value, e.g. 5000 K. If the light still matches, the unit is right. Otherwise switch the unit and repeat.

More notes:

- The offset only applies when the color temperature is set through a room light. Controlling the light directly gives it the value without the offset.
- The resulting value is limited to the light's range.
- The room light shows the requested value, not the shifted one.
- The settings do not show up on dashboards and are not affected by area actions.
- Lights that only support colors have no offset.

## Schedule

Every room follows one color temperature schedule, which follows the sun. The sun is computed from the location in Home Assistant's settings; solar noon moves with daylight saving time by itself.

| When | Color temperature |
|---|---|
| Sunrise, not before 06:00 and not after 07:30 | Warmest (2700 K) |
| Solar noon | Coldest (5000 K) |
| Sunset | Two thirds of the way back to the warmest (3200 K) |
| Sunset + 1:30, at the latest 22:00 | Warmest (2700 K) |

At night it holds the warmest value. Between these points, the color temperature changes the way daylight does: fast after sunrise and before sunset, slowly around noon. The change runs in mired, matching how the eye perceives color temperature. The warmest and coldest value are settings, see "Settings".

Every room has a **Schedule** switch (`switch.schedule_<area>`). It is on while the room follows the schedule:

- Turn it off to stop the schedule in the room. It stays off, also across restarts, until you turn it on again.
- A manual change turns it off for a while, see "Manual changes".

**Target color temperature** (`sensor.target_color_temperature`) shows the schedule's current value, the same for all rooms, e.g. for your own automations.

How the schedule behaves:

- **Turning on through the room light:** lights that are off turn on with the target right away, without showing their old color first. With a color or color temperature of its own (e.g. from a scene), that one is kept; see "Manual changes".
- **Turning on at the light itself or with a wall switch:** the light gets the target within about a second.
- **Ongoing updates:** every minute, lights that are on get the target with a short 2 s transition. A command is only sent if the light is at least 50 K away from it. The rooms are spread across the minute.
- **Colored lights:** lights that currently show a color are left alone.
- **Brightness:** the schedule only changes the color temperature. The brightness is up to you.
- **Offsets:** color temperature offsets apply to the schedule, too.
- **Range:** a light that cannot reach the target gets the closest value it supports, e.g. 2900 K for an Elgato Key Light when the warmest value is set to 2700 K.

## Settings

Light Buddy → **Configure** → **Settings** shows the schedule as a chart: today, and thin lines for the longest and the shortest day of the year. It shows the saved values; after changing them, open the settings again to see the new curve.

| Setting | Default | Meaning |
|---|---|---|
| Warmest color temperature | 2700 K | At night, in the early morning and in the late evening. |
| Coldest color temperature | 5000 K | At solar noon. At least 500 K above the warmest. |
| Smallest change | 50 K | Lights that are on only get a new color temperature once the target differs at least this much. |
| Transition | 2 s | For the updates every minute. IKEA lights ignore other commands during a transition, so keep it short. |
| Resume after | 0 (never) | After a manual change, the Schedule switch turns on again by itself after this time. |
| Separate brightness for IKEA lights | Off | For lights that do not change brightness and color temperature together cleanly, e.g. when turning on a room with a brightness: the color temperature first, the brightness half a second after the transition. "IKEA lights only" recognizes them by the device's manufacturer. |

## Manual changes

When you set a color temperature or color by hand, the schedule leaves the color in that room alone: the room's **Schedule** switch turns off. Dimming is no such change: the brightness is yours anyway, and the color temperature keeps following the schedule.

These count as manual:

- a color temperature or color through the room light (tile, slider, scene);
- a command with a color temperature or color to a single light, e.g. through its own tile, an automation or a scene;
- a change outside Home Assistant, e.g. with a remote paired directly to a light. It counts from a difference of 150 K to what Light Buddy sent last, or when the light switches to a color.

Turning on without values is no manual change.

The switch turns on again by itself:

- when all lights in the room are off; the next turn on follows the schedule again;
- optionally after the time set in **Resume after**.

Turning it on yourself applies the schedule again right away.

## Disco

Every room has a **Disco** switch (`switch.disco_<area>`). While it is on:

- **Colored lights** cycle through a palette, each light showing a different color. Lights that were off turn on with the room's brightness, or at full brightness if the room was off.
- **White lights** (and other lights without color) turn off.
- **Brightness** can still be changed through the room light; it only reaches the colored lights.
- **The schedule** waits and sends nothing.

Turning disco off restores what the lights showed before, including the white lights. Lights that followed the schedule get its current color temperature right away.

Disco also ends:

- when the room is turned off; the lights stay off;
- when you set a color or color temperature through the room light; the lights keep it, and the Schedule switch turns off as for any manual change.

Turning disco off waits for a running fade to finish, at most about 1.5 s. Lights switched on or off by hand during disco, against what disco did with them, keep that: a white light turned on at the wall stays on.

After a restart of Home Assistant, or a reload of Light Buddy, disco is off and the lights stay as they are.

Speed and palette are set for all rooms under **Configure** → **Settings**:

| Setting | Default | Meaning |
|---|---|---|
| Disco speed | 4 s | Seconds until the next color, between 2 and 60. Each color fades in over 1 s, kept short because IKEA lights ignore commands while fading. Every change sends one command per color, so very short times load the Zigbee network. |
| Disco palette | Rainbow | Rainbow, Party, Fire or Ocean. |

## Behavior

- **State:** the room is on as soon as one light is on. Brightness and color temperature are the average of the lights that are on.
- **Colors:** colors only go to lights that support color. White lights only get the brightness.
- **Zigbee network:**
  - Lights with the same values share one call.
  - Each room sends at most one command every 0.3 s. While dragging a slider, only the latest value is sent.
  - Turning off is only sent to lights that are not off already.
- **Area:** Light Buddy's entities are deliberately not assigned to an area. Otherwise area actions like "turn off all lights in the living room" would control every light twice.

## Development

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check .
```

### Icon

The icon is an SVG in `assets/icon.svg`. To render it again (macOS, uses QuickLook):

```sh
.venv/bin/python assets/render_icon.py assets/icon.svg
cp assets/icon.256.png custom_components/light_buddy/brand/icon.png
cp assets/icon.512.png custom_components/light_buddy/brand/icon@2x.png
```

Home Assistant shows the images from `custom_components/light_buddy/brand/` directly.

## License

MIT, see [LICENSE](LICENSE).
