#!/usr/bin/env python3
"""One controller per session; newline JSON commands/status over stdin/stdout."""
import ctypes
import fcntl
import json
import math
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import time


FADE_INTERVAL = 0.025


def clamp(value, low, high):
    return max(low, min(high, value))


def brightness_target(lux, offset):
    lux = max(0, lux)
    # The lid sensor sees just 2.5–5 lux with the desk lamp behind the laptop.
    # Raise that range without adding the same amount in complete darkness.
    low_light_boost = 32 * -math.expm1(-lux / 1.5)
    daylight_growth = 24 * math.log10(1 + lux / 5)
    return clamp(round(18 + low_light_boost + daylight_growth + offset), 5, 100)


def color_target(raw):
    # UM3406HA lamp comparison: raw is Kelvin-like despite advertised scale 0.1.
    return clamp(raw - 550, 4000, 6500)


class Hardware:
    def __init__(self):
        self.sensor = None
        self.backlight = None
        self.discover()

    def discover(self):
        self.sensor = next((p for p in Path('/sys/bus/iio/devices').glob('iio:device*')
                            if (p / 'name').read_text().strip() == 'als'), None)
        devices = list(Path('/sys/class/backlight').glob('*'))
        self.backlight = next((p for p in devices if p.name.startswith('amdgpu')), next(iter(devices), None))

    @staticmethod
    def number(path):
        value = float(path.read_text())
        if not math.isfinite(value):
            raise ValueError('Sensor returned an invalid number')
        return value

    def sample(self):
        if not self.sensor or not self.sensor.exists():
            self.discover()
        if not self.sensor:
            raise RuntimeError('Ambient light sensor unavailable')
        lux = ((self.number(self.sensor / 'in_illuminance_raw') +
                self.number(self.sensor / 'in_illuminance_offset')) *
               self.number(self.sensor / 'in_illuminance_scale'))
        raw = self.number(self.sensor / 'in_colortemp_raw')
        if lux < 0 or not 1000 <= raw <= 25000:
            raise ValueError('Ambient sensor reading is out of range')
        return lux, raw

    def brightness(self):
        if not self.backlight:
            raise RuntimeError('Screen brightness unavailable')
        return (int(self.number(self.backlight / 'brightness')),
                int(self.number(self.backlight / 'max_brightness')))

    @staticmethod
    def run(*args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=3)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'Command failed: ' + args[0])
        return result.stdout.strip()

    def set_brightness(self, value):
        self.run('brightnessctl', '-q', '-d', self.backlight.name, 'set', str(int(value)))

    def temperature(self):
        try:
            return int(self.run('hyprctl', 'hyprsunset', 'temperature'))
        except (RuntimeError, ValueError):
            return None

    def ensure_color(self):
        if self.temperature() is not None:
            return
        # Direct child with detached stdio; no terminal or privileged service needed.
        if subprocess.run(['pgrep', '-x', 'hyprsunset'], stdout=subprocess.DEVNULL).returncode:
            subprocess.Popen(['hyprsunset', '--identity'], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        for _ in range(15):
            time.sleep(0.2)
            if self.temperature() is not None:
                return
        raise RuntimeError('Could not connect to hyprsunset')

    def set_temperature(self, kelvin):
        self.run('hyprctl', 'hyprsunset', 'temperature', str(round(kelvin)))

    def identity(self):
        # identity removes the transform but leaves the temperature getter stale.
        # Normalize that getter for Omarchy's Night Light status, then remove tint.
        try:
            self.set_temperature(6500)
        finally:
            self.run('hyprctl', 'hyprsunset', 'identity')

    def screen_awake(self):
        monitors = json.loads(self.run('hyprctl', '-j', 'monitors'))
        return any(m.get('dpmsStatus', True) for m in monitors if m.get('name', '').startswith('eDP'))


class Controller:
    def __init__(self, hardware, state_path):
        self.hw = hardware
        self.path = state_path
        self.state = {'autoBrightness': False, 'autoColor': False, 'offset': 0,
                      'savedBrightness': None, 'lastBrightness': None, 'lastColor': None}
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            self.state.update({k: saved[k] for k in self.state if k in saved})
        self.state['offset'] = clamp(float(self.state['offset']), -40, 40)
        self.idle = False
        self.color_paused = False
        self.resume_cooldown = 0
        self.manual_until = 0
        self.lux = None
        self.raw_kelvin = None
        self.smoothed_color = None
        self.current_brightness = None
        self.error = ''
        self.failures = 0
        self.quit = False
        self.first_color_sample = True
        self.brightness_fade = None
        self.brightness_goal = None
        self.next_color_tick = 0

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, indent=2) + '\n')
        temporary.replace(self.path)

    def restore_brightness(self):
        self.cancel_fade()
        saved = self.state['savedBrightness']
        if saved is not None:
            _, maximum = self.hw.brightness()
            self.hw.set_brightness(clamp(saved, 1, maximum))
            self.state['savedBrightness'] = None
            self.state['lastBrightness'] = None
            self.current_brightness = saved * 100 / maximum

    def release_color(self):
        last = self.state['lastColor']
        # Preserve an external Night Light/manual adjustment made after our write.
        if last is not None and self.hw.temperature() == last:
            self.hw.identity()
        self.state['lastColor'] = None
        self.smoothed_color = None
        self.color_paused = False

    def command(self, command):
        if command.get('quit'):
            self.quit = True
            return
        self.error = ''
        if 'idle' in command:
            if self.idle and command['idle'] is not True:
                # Let stock wake handlers finish restoring brightness first.
                self.resume_cooldown = time.monotonic() + 3
            self.idle = command['idle'] is True
            if self.idle:
                self.cancel_fade()
        if 'offset' in command:
            value = float(command['offset'])
            if not math.isfinite(value):
                raise ValueError('Invalid brightness adjustment')
            self.state['offset'] = clamp(round(value), -40, 40)
        if 'autoBrightness' in command:
            enabled = command['autoBrightness'] is True
            if enabled and not self.state['autoBrightness']:
                self.cancel_fade()
                current, _ = self.hw.brightness()
                self.state['savedBrightness'] = current
                self.state['lastBrightness'] = current
                self.manual_until = 0
            elif not enabled:
                self.restore_brightness()
            self.state['autoBrightness'] = enabled
        if 'autoColor' in command:
            enabled = command['autoColor'] is True
            if enabled and not self.state['autoColor']:
                self.next_color_tick = 0
                self.hw.ensure_color()
                self.color_paused = False
                self.smoothed_color = None
                current = self.hw.temperature()
                # Existing Night Light keeps ownership until it is turned off.
                if self.state['lastColor'] is None and current is not None and current < 6000:
                    self.color_paused = True
            elif not enabled:
                self.release_color()
            self.state['autoColor'] = enabled
        self.save()

    def cancel_fade(self):
        self.brightness_fade = None
        self.brightness_goal = None

    def observe_brightness(self, current, maximum):
        self.current_brightness = current * 100 / maximum
        last = self.state['lastBrightness']
        if last is not None and current != last:
            # Notice manual changes even between sensor samples/fade frames.
            self.manual_until = time.monotonic() + 10
            self.cancel_fade()
        self.state['lastBrightness'] = current

    def fade_step(self):
        if self.brightness_fade is None:
            return
        if self.quit or self.idle or not self.state['autoBrightness']:
            self.cancel_fade()
            return
        current, maximum = self.hw.brightness()
        self.observe_brightness(current, maximum)
        if self.brightness_fade is None:
            return
        start, source, target, duration = self.brightness_fade
        progress = clamp((time.monotonic() - start) / duration, 0, 1)
        # Smoothstep eases both ends, with hardware-sized steps every 25 ms.
        eased = progress * progress * (3 - 2 * progress)
        value = clamp(round((source + (target - source) * eased) * maximum / 100), 1, maximum)
        if value != current:
            self.hw.set_brightness(value)
            self.state['lastBrightness'] = value
            self.current_brightness = value * 100 / maximum
        if progress >= 1:
            self.brightness_fade = None
            self.save()

    def tick(self):
        self.error = ''
        try:
            self.lux, self.raw_kelvin = self.hw.sample()
            self.failures = 0
        except (OSError, ValueError, RuntimeError) as e:
            self.failures += 1
            if self.failures == 5:
                self.restore_brightness()
                self.release_color()
                self.save()
            raise RuntimeError(str(e)) from e
        current, maximum = self.hw.brightness() if self.hw.backlight else (0, 1)
        self.current_brightness = current * 100 / maximum
        if not (self.state['autoBrightness'] or self.state['autoColor']):
            return
        if self.idle or time.monotonic() < self.resume_cooldown or not self.hw.screen_awake():
            self.cancel_fade()
            return

        if self.state['autoBrightness']:
            if self.state['savedBrightness'] is None:
                self.state['savedBrightness'] = current
            self.observe_brightness(current, maximum)
            target = brightness_target(self.lux, self.state['offset'])
            if current > 0 and time.monotonic() >= self.manual_until:
                reference = self.brightness_goal if self.brightness_goal is not None else self.current_brightness
                if abs(target - reference) >= 2:
                    duration = 1.0 if target > self.current_brightness else 3.0
                    self.brightness_goal = target
                    self.brightness_fade = (time.monotonic(), self.current_brightness, target, duration)
                    self.save()

        if self.state['autoColor']:
            if time.monotonic() < self.next_color_tick:
                return
            self.next_color_tick = time.monotonic() + 2
            current_color = self.hw.temperature()
            if current_color is None:
                self.hw.ensure_color()
                current_color = self.hw.temperature()
                self.state['lastColor'] = None
            last = self.state['lastColor']
            if self.first_color_sample and last is None and current_color is not None and current_color < 6000:
                self.color_paused = True
            self.first_color_sample = False
            if last is not None and current_color != last:
                self.color_paused = True
                self.state['lastColor'] = None
            if self.color_paused:
                if current_color is None or current_color < 6000:
                    return
                self.color_paused = False
                self.smoothed_color = None
            target = color_target(self.raw_kelvin)
            initial = current_color if current_color is not None else 6500
            self.smoothed_color = (initial * 0.9 + target * 0.1 if self.smoothed_color is None
                                   else self.smoothed_color * 0.9 + target * 0.1)
            proposed = round(self.smoothed_color)
            if current_color is None or abs(proposed - current_color) >= 100:
                self.hw.set_temperature(proposed)
                self.state['lastColor'] = proposed
                self.save()

    def status(self):
        return {'ready': self.lux is not None,
                'autoBrightness': self.state['autoBrightness'], 'autoColor': self.state['autoColor'],
                'offset': self.state['offset'], 'lux': self.lux,
                'ambientKelvin': round(self.raw_kelvin - 550) if self.raw_kelvin is not None else None,
                'targetKelvin': round(self.smoothed_color) if self.smoothed_color is not None else None,
                'brightness': self.current_brightness, 'brightnessAvailable': self.hw.backlight is not None,
                'colorAvailable': shutil.which('hyprsunset') is not None,
                'colorPaused': self.color_paused, 'idle': self.idle, 'error': self.error,
                'sensorClipped': self.raw_kelvin == 7000}

    def cleanup(self):
        for restore in (self.restore_brightness, self.release_color):
            try:
                restore()
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                print('Could not restore display: ' + str(e), file=sys.stderr)
        self.save()


def main():
    state_dir = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'ambient-display'
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / 'controller.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Ambient Display controller is already running', file=sys.stderr)
            return 1
        controller = Controller(Hardware(), state_dir / 'state.json')
        def stop(signum, frame):
            controller.quit = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        # Restore owned settings if the shell exits unexpectedly too.
        ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0)
        pending = b''
        next_tick = 0
        next_frame = 0
        try:
            while not controller.quit:
                if time.monotonic() >= next_tick:
                    try:
                        controller.tick()
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
                        controller.error = str(e)
                    next_tick = time.monotonic() + (1 if controller.state['autoBrightness'] else 2)
                    try:
                        print(json.dumps(controller.status()), flush=True)
                    except BrokenPipeError:
                        break
                if controller.brightness_fade is not None and time.monotonic() >= next_frame:
                    try:
                        controller.fade_step()
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
                        controller.error = str(e)
                        controller.cancel_fade()
                    next_frame = time.monotonic() + FADE_INTERVAL
                deadline = min(next_tick, next_frame) if controller.brightness_fade is not None else next_tick
                readable, _, _ = select.select([sys.stdin], [], [], max(0, deadline - time.monotonic()))
                if readable:
                    data = os.read(sys.stdin.fileno(), 65536)
                    if not data:
                        break
                    pending += data
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        try:
                            controller.command(json.loads(line))
                        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
                            controller.error = str(e)
                        try:
                            print(json.dumps(controller.status()), flush=True)
                        except BrokenPipeError:
                            controller.quit = True
                            break
        finally:
            controller.cleanup()
    return 0


if __name__ == '__main__':
    sys.exit(main())
