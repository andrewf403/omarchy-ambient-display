# Ambient Display

Personal Omarchy plugin for the Zenbook UM3406HA. Click its sun icon to open
Auto brightness, Auto color temperature, and Brightness adjustment.
Both automatic controls start disabled. The switches and adjustment persist
under `~/.local/state/ambient-display/state.json`.

## Installation

With GitHub CLI authenticated to an account that can access this private repo:

```sh
gh repo clone andrewf403/omarchy-ambient-display ~/.config/omarchy/plugins/andrewf.ambient-display
omarchy plugin validate ~/.config/omarchy/plugins/andrewf.ambient-display
omarchy plugin enable andrewf.ambient-display --section right --before omarchy.monitor
```

The destination directory must not already exist. Click the sun icon in the
bar to enable either automatic control. Removing the bar entry with
`omarchy plugin disable andrewf.ambient-display` unloads the controller.

## Behavior

The brightness adjustment adds -40 to +40 percentage points to a curve tuned
for the lid sensor's shadowed view of the desk lamp, limited to 5–100%.
At zero adjustment it starts at 18% in darkness, rises steeply through 0–3 lux,
then grows gradually into brighter light. Approximate targets:

| Sensor lux | Screen brightness |
| --- | --- |
| 0 | 18% |
| 1 | 35% |
| 2.5 | 48% |
| 3 | 51% |
| 5 | 56% |
| 10 | 61% |
| 100 | 82% |
| 1000 | 100% |

The curve is `18 + 32 * (1 - exp(-lux / 1.5)) + 24 * log10(1 + lux / 5)`.
It compensates for this desk arrangement; it cannot determine light direction
or the actual desk illuminance from a single sensor reading.
The sensor is checked every second while automatic
brightness is enabled. Brightening fades take 1 second; dimming fades take
3 seconds, with eased steps every 25 ms and a 2-point target deadband to avoid
chasing sensor noise. Manual brightness changes pause automation for 10 seconds.
Disabling automatic brightness restores the exact pre-enable brightness;
ordinary brightness keys and the stock display panel remain available.

Color follows `in_colortemp_raw - 550`, limited to 4000–6500 K, with roughly
20-second smoothing and 100 K update thresholds. This is a provisional
desk-specific comparison against a BenQ Halo 2, not an absolute calibration.
The UM3406HA's advertised 0.1 color scale is intentionally bypassed. Its
chromaticity fields read zero; raw color 7000 may be clipped.

Disabling automatic color uses `hyprctl hyprsunset identity` to remove the tint.
If an external control changes the temperature, automation pauses and leaves
that change intact, including on disable. Returning to 6000 K or above resumes
adaptation. Omarchy's Night Light indicator still identifies warmth from
temperature and can therefore show as enabled during ambient adaptation.
An external command setting exactly our current temperature cannot be
distinguished from our own setting through hyprsunset's temperature getter.

One background controller is shared across bar instances. It pauses display
writes after 120 seconds of idle (respecting inhibitors) and while the internal
screen is off. Removing/disabling the plugin or closing the shell restores
owned display settings; saved switch choices remain for the next load.
Hyprsunset affects the outputs it manages, which may include external screens;
brightness control targets only the laptop backlight.

Dependencies: Python 3, brightnessctl, hyprsunset, hyprctl, Quickshell (all
already present on this machine). No root service or privileged helper.

IPC examples:

```sh
omarchy-shell andrewf.ambient-display toggle
omarchy-shell andrewf.ambient-display.control status
omarchy-shell andrewf.ambient-display.control brightness true
omarchy-shell andrewf.ambient-display.control color true
omarchy-shell andrewf.ambient-display.control offset 10
```

Tests: `python test_controller.py`.
