<p align="center">
  <img src="custom_components/light_buddy/brand/icon@2x.png" alt="Light Buddy" width="128">
</p>

# Light Buddy

[![Validate](https://github.com/hoiz/light-buddy/actions/workflows/validate.yml/badge.svg)](https://github.com/hoiz/light-buddy/actions/workflows/validate.yml)
[![Tests](https://github.com/hoiz/light-buddy/actions/workflows/tests.yml/badge.svg)](https://github.com/hoiz/light-buddy/actions/workflows/tests.yml)

Four things always bothered me about lights in Home Assistant:

1. **Rooms aren't lights.** All my lights are assigned to areas, yet to control a room as one light I still had to build a light group or a template, and keep it up to date.
2. **Lamps don't agree on color temperature.** Some lamps look clearly colder or warmer than others at the same setting, and there is no way to correct that.
3. **No automatic color temperature works the way I want it to.**
4. **There is no disco mode.** :)

Light Buddy fixes all four.

## Installation

Requires Home Assistant 2026.10 or newer.

**HACS:** HACS → three-dot menu → Custom repositories → add `https://github.com/hoiz/light-buddy` with the type "Integration". Then install "Light Buddy" and restart Home Assistant.

**Manually:** copy the folder `custom_components/light_buddy` into the `custom_components` folder of your Home Assistant configuration and restart Home Assistant.

Then add it: Settings → Devices & services → Add integration → **Light Buddy**. There is nothing else to set up; the rooms are created automatically.

## 1. One light per room

For every area with at least one light, Light Buddy creates a light, e.g. `light.all_living` named "All lights in Living". It switches and dims all lights of the area together and works in a regular dashboard tile with a brightness slider.

**Which lights belong to a room:**

- lights whose entity is assigned to the area;
- lights without an area of their own whose device is in the area;
- not included: disabled entities, light groups (`group`) and Light Buddy's own entities.

When you move a light, or add or rename an area, Light Buddy follows without a restart. Areas without lights lose their room light.

**Exclusions:** Light Buddy → **Configure** → **Exclusions**:

- **Excluded areas** get no room light.
- **Excluded lights** belong to no room, e.g. template lights that control other lights themselves.

**How the room light behaves:**

- **State:** the room is on as soon as one light is on. Brightness and color temperature are the average of the lights that are on.
- **Colors** only go to lights that support color. White lights only get the brightness.
- **Zigbee friendly:**
  - Lights with the same values share one call.
  - Each room sends at most one command every 0.3 s; while dragging a slider, only the latest value is sent.
  - Turning off only goes to lights that are not off already.
  - For lights that don't change brightness and color temperature together cleanly, IKEA ones in particular, **Separate brightness for IKEA lights** (under Settings) sends the color temperature first and the brightness half a second after the transition.
- **No area:** Light Buddy's entities are deliberately not assigned to an area. Otherwise area actions like "turn off all lights in the living room" would control every light twice.

## 2. Color temperature offset

For a lamp that looks colder or warmer than the others. Every light with color temperature in a room gets two settings on its device page, under **Configuration**:

- **Color temperature offset**: a slider for the offset.
- **Color temperature offset unit**: Kelvin or mired. Switching converts the offset so the light looks the same at 3000 K.

**Kelvin or mired?** A Kelvin offset is strong in warm light and barely noticeable in cold light: −300 K is clearly visible at 2700 K and almost invisible at 6000 K. A mired offset looks equally strong everywhere, matching how the eye perceives color temperature. In Kelvin, a negative value makes the light warmer; in mired, a positive one does.

**Try it out:** turn the room on; every change of the slider applies to the light right away.

1. Set the room to a warm value, e.g. 2700 K, and adjust the offset until the light matches the others.
2. Set the room to a cold value, e.g. 5000 K. If the light still matches, the unit is right. Otherwise switch the unit and repeat.

Notes:

- The offset applies when the color temperature is set through a room light or by the schedule. Controlling the light directly gives it the value without the offset.
- The resulting value is limited to the light's range.
- The room light shows the requested value, not the shifted one.
- Lights that only support colors have no offset.

## 3. Color temperature that follows the sun

Every room follows one color temperature schedule, which follows the sun. The sun is computed from the location in Home Assistant's settings; solar noon moves with daylight saving time by itself.

| When | Color temperature |
|---|---|
| Sunrise, not before 06:00 and not after 07:30 | Warmest (2700 K) |
| Solar noon | Coldest (5000 K) |
| Sunset | Two thirds of the way back to the warmest (3200 K) |
| Sunset + 1:30, at the latest 22:00 | Warmest (2700 K) |

At night it holds the warmest value. Between these points, the color temperature changes the way daylight does: fast after sunrise and before sunset, slowly around noon. The change runs in mired, matching how the eye perceives color temperature.

**Entities:**

- **Schedule** (`switch.schedule_<area>`), one per room: on while the room follows the schedule. Turn it off to stop the schedule in that room; it stays off, also across restarts, until you turn it on again.
- **Target color temperature** (`sensor.target_color_temperature`): the schedule's current value, the same for all rooms, e.g. for your own automations.

**How it behaves:**

- **Turning on through the room light:** lights that are off turn on with the target right away, without showing their old color first. A color or color temperature of its own (e.g. from a scene) is kept, see "Manual changes".
- **Turning on at the light itself or with a wall switch:** the light gets the target within about a second.
- **Ongoing updates:** every minute, lights that are on get the target with a short transition, if they are at least the smallest change away from it. The rooms are spread across the minute.
- **Colored lights** are left alone.
- **Brightness** is up to you; the schedule only changes the color temperature.
- **Range:** a light that cannot reach the target gets the closest value it supports, e.g. 2900 K for an Elgato Key Light when the warmest value is set to 2700 K.

**Manual changes:** when you set a color temperature or color by hand, the schedule leaves that room alone and its **Schedule** switch turns off. Dimming is no such change. These count as manual:

- a color temperature or color through the room light (tile, slider, scene);
- a command with a color temperature or color to a single light, e.g. through its own tile, an automation or a scene;
- a change outside Home Assistant, e.g. with a remote paired directly to a light. It counts from a difference of 150 K to what Light Buddy sent last, or when the light switches to a color.

The switch turns on again by itself when all lights in the room are off, and optionally after **Resume after**. Turning it on yourself applies the schedule right away.

**Settings:** Light Buddy → **Configure** → **Settings** shows the curve as a chart: today, and thin lines for the longest and the shortest day of the year. It shows the saved values; after changing them, open the settings again to see the new curve.

| Setting | Default | Meaning |
|---|---|---|
| Warmest color temperature | 2700 K | At night, in the early morning and in the late evening. |
| Coldest color temperature | 5000 K | At solar noon. At least 500 K above the warmest. |
| Smallest change | 50 K | Lights that are on only get a new color temperature once the target differs at least this much. |
| Transition | 2 s | For the updates every minute. IKEA lights ignore other commands during a transition, so keep it short. |
| Resume after | 0 (never) | After a manual change, the Schedule switch turns on again by itself after this time. |

## 4. Disco mode

Every room has a **Disco** switch (`switch.disco_<area>`). While it is on:

- **Colored lights** cycle through a palette, each light showing a different color. Lights that were off turn on with the room's brightness, or at full brightness if the room was off.
- **White lights** (and other lights without color) turn off.
- **Brightness** can still be changed through the room light; it only reaches the colored lights.
- **The schedule** waits and sends nothing.

Turning disco off restores what the lights showed before, including the white lights. Lights that followed the schedule get its current color temperature right away. It waits for a running fade to finish, at most about 1.5 s. Lights switched on or off by hand during disco, against what disco did with them, keep that: a white light turned on at the wall stays on.

Disco also ends when the room is turned off (the lights stay off), or when you set a color or color temperature through the room light (the lights keep it, and the Schedule switch turns off as for any manual change). After a restart of Home Assistant, or a reload of Light Buddy, disco is off and the lights stay as they are.

Without Zigbee groups, lights never change exactly together; expect a small offset between them.

Speed and palette are set for all rooms under **Configure** → **Settings**:

| Setting | Default | Meaning |
|---|---|---|
| Disco speed | 4 s | Seconds until the next color, between 2 and 60. Each color fades in over 1 s, kept short because IKEA lights ignore commands while fading. Every change sends one command per color, so very short times load the Zigbee network. |
| Disco palette | Rainbow | Rainbow, Party, Fire or Ocean. |

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
