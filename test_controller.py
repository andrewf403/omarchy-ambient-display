import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('controller', Path(__file__).with_name('controller.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Hardware:
    backlight = True

    def __init__(self):
        self.value, self.maximum, self.temp = 38, 100, 6500
        self.lux, self.raw = 100, 4550
        self.writes = []
        self.awake = True
        self.failed = False

    def sample(self):
        if self.failed:
            raise OSError('sensor disconnected')
        return self.lux, self.raw

    def brightness(self): return self.value, self.maximum
    def set_brightness(self, value):
        self.writes.append(('brightness', value))
        self.value = value
    def temperature(self): return self.temp
    def ensure_color(self): pass
    def set_temperature(self, value):
        self.writes.append(('temperature', value))
        self.temp = value
    def identity(self):
        self.writes.append(('identity',))
        self.temp = 6500
    def screen_awake(self): return self.awake


class Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hw = Hardware()
        self.c = module.Controller(self.hw, Path(self.directory.name) / 'state.json')
        self.clock = patch.object(module.time, 'monotonic', return_value=0).start()
        self.addCleanup(patch.stopall)

    def advance(self, seconds):
        self.clock.return_value += seconds
        self.c.fade_step()

    def color_ticks(self, count):
        for _ in range(count):
            self.clock.return_value += 2
            self.c.tick()

    def steady_color(self, kelvin=5000):
        self.hw.temp = kelvin
        self.hw.raw = kelvin + 550
        # Simulate an already adapted controller that owns its current setting.
        self.c.state['autoColor'] = True
        self.c.state['lastColor'] = kelvin
        self.color_ticks(5)
        self.hw.writes.clear()

    def test_disabled_controller_only_observes(self):
        self.c.tick()
        self.c.cleanup()
        self.assertEqual(self.hw.writes, [])

    def test_identity_normalizes_getter_then_removes_transform(self):
        hw = object.__new__(module.Hardware)
        hw.run = Mock(return_value='')
        hw.identity()
        self.assertEqual([call.args for call in hw.run.call_args_list], [
            ('hyprctl', 'hyprsunset', 'temperature', '6500'),
            ('hyprctl', 'hyprsunset', 'identity')])

    def test_restart_preserves_external_nightlight(self):
        self.c.command({'autoColor': True})
        self.c.cleanup()
        other = module.Controller(self.hw, self.c.path)
        self.hw.temp = 4000
        self.hw.writes = []
        other.tick()
        self.assertTrue(other.color_paused)
        self.assertEqual(self.hw.writes, [])

    def test_brightness_restores_exact_pre_enable_value(self):
        self.c.command({'autoBrightness': True})
        self.c.tick()
        self.advance(0.5)
        self.assertNotEqual(self.hw.value, 38)
        self.c.command({'autoBrightness': False})
        self.assertEqual(self.hw.value, 38)

    def test_offset_is_persistent_and_bounded(self):
        self.c.command({'offset': 200})
        other = module.Controller(self.hw, self.c.path)
        self.assertEqual(other.state['offset'], 40)
        self.assertEqual(module.brightness_target(10, 20) - module.brightness_target(10, 0), 20)

    def test_curve_separates_darkness_from_the_shadowed_desk_lamp(self):
        self.assertEqual(module.brightness_target(0, 0), 18)
        for lux in (2.5, 3, 5):
            self.assertGreaterEqual(module.brightness_target(lux, 0), 48)
            self.assertLessEqual(module.brightness_target(lux, 0), 56)
        self.assertGreaterEqual(module.brightness_target(100, 0), 80)

    def test_curve_rises_monotonically_and_levels_out(self):
        values = [module.brightness_target(i / 10, 0) for i in range(10001)]
        self.assertTrue(all(a <= b for a, b in zip(values, values[1:])))
        low_light_rise = module.brightness_target(3, 0) - module.brightness_target(0, 0)
        higher_light_rise = module.brightness_target(6, 0) - module.brightness_target(3, 0)
        self.assertGreater(low_light_rise, higher_light_rise * 3)
        self.assertEqual(module.brightness_target(-1, -40), 5)
        self.assertEqual(module.brightness_target(100000, 40), 100)

    def test_own_warm_temperature_does_not_pause_itself(self):
        self.c.command({'autoColor': True})
        for _ in range(20):
            self.clock.return_value += 2
            self.c.tick()
        self.assertLess(self.hw.temp, 6000)
        self.assertFalse(self.c.color_paused)
        self.c.command({'autoColor': False})
        self.assertEqual(self.hw.writes[-1], ('identity',))

    def test_nightlight_override_is_preserved_on_disable(self):
        self.c.command({'autoColor': True})
        self.color_ticks(5)
        self.hw.temp = 4000
        self.clock.return_value += 2
        self.c.tick()
        self.assertTrue(self.c.color_paused)
        self.c.command({'autoColor': False})
        self.assertEqual(self.hw.temp, 4000)

    def test_existing_nightlight_pauses_until_turned_off(self):
        self.hw.temp = 4000
        self.c.command({'autoColor': True})
        self.c.tick()
        self.assertTrue(self.c.color_paused)
        self.assertEqual(self.hw.writes, [])
        self.hw.temp = 6500
        self.color_ticks(5)
        self.assertFalse(self.c.color_paused)
        self.assertNotEqual(self.hw.writes, [])

    def test_color_waits_for_a_full_window_and_rejects_startup_spikes(self):
        self.c.command({'autoColor': True})
        for raw in (4550, 4550, 7050, 4550):
            self.hw.raw = raw
            self.color_ticks(1)
        self.assertEqual(self.hw.writes, [])
        self.hw.raw = 4550
        self.color_ticks(1)
        self.assertEqual(self.hw.temp, 6250)
        self.assertEqual(self.c.accepted_color, 4000)

    def test_color_rejects_two_large_outliers(self):
        self.steady_color()
        for raw in (7050, 7050, 5550, 5550, 5550, 5550):
            self.hw.raw = raw
            self.color_ticks(1)
        self.assertEqual(self.hw.writes, [])
        self.assertEqual(self.hw.temp, 5000)

    def test_small_color_noise_does_not_move_the_accepted_target(self):
        self.steady_color()
        for raw in [5700, 5650, 5400, 5450, 5600, 5500] * 5:
            self.hw.raw = raw
            self.color_ticks(1)
        self.assertEqual(self.hw.writes, [])
        self.assertEqual(self.c.accepted_color, 5000)

    def test_color_requires_a_300_kelvin_median_change(self):
        self.steady_color()
        self.hw.raw = 5849
        self.color_ticks(10)
        self.assertEqual(self.hw.writes, [])
        self.hw.raw = 5850
        self.color_ticks(3)
        self.assertEqual(self.c.accepted_color, 5300)
        self.color_ticks(10)
        self.assertGreaterEqual(self.hw.temp, 5100)
        self.assertLessEqual(self.hw.temp, 5300)

    def test_sustained_color_change_is_followed_gradually(self):
        self.steady_color()
        self.hw.raw = 6550
        self.color_ticks(2)
        self.assertEqual(self.hw.writes, [])
        self.color_ticks(1)
        self.assertEqual(self.hw.temp, 5100)
        self.color_ticks(45)
        self.assertGreaterEqual(self.hw.temp, 5900)
        self.assertLessEqual(self.hw.temp, 6000)
        self.assertFalse(self.c.color_paused)

    def test_color_does_not_sample_the_same_instant_more_than_once(self):
        self.c.command({'autoColor': True})
        for _ in range(5): self.c.tick()
        self.assertEqual(len(self.c.color_samples), 1)
        self.assertEqual(self.hw.writes, [])

    def test_manual_nightlight_during_color_warmup_is_preserved(self):
        self.c.command({'autoColor': True})
        self.color_ticks(1)
        self.hw.temp = 4000
        self.color_ticks(10)
        self.assertTrue(self.c.color_paused)
        self.assertEqual(self.hw.writes, [])
        self.c.command({'autoColor': False})
        self.assertEqual(self.hw.temp, 4000)

    def test_idle_wake_uses_fresh_color_samples(self):
        self.steady_color()
        self.c.command({'idle': True})
        self.hw.raw = 6550
        self.color_ticks(10)
        self.assertEqual(len(self.c.color_samples), 0)
        self.c.command({'idle': False})
        self.clock.return_value += 4
        self.color_ticks(4)
        self.assertEqual(self.hw.writes, [])
        self.color_ticks(1)
        self.assertEqual(self.hw.temp, 5100)

    def test_sensor_error_discards_old_color_samples(self):
        self.steady_color()
        self.hw.failed = True
        with self.assertRaises(RuntimeError): self.c.tick()
        self.assertEqual(len(self.c.color_samples), 0)
        self.hw.failed = False
        self.hw.raw = 6550
        self.color_ticks(4)
        self.assertEqual(self.hw.writes, [])

    def test_idle_and_dpms_do_not_change_display(self):
        self.c.command({'autoBrightness': True, 'autoColor': True})
        self.c.command({'idle': True})
        self.c.tick()
        self.assertEqual(self.hw.writes, [])
        self.c.command({'idle': False})
        self.hw.awake = False
        self.c.tick()
        self.assertEqual(self.hw.writes, [])

    def test_manual_brightness_gets_temporary_override(self):
        self.c.command({'autoBrightness': True})
        self.c.tick()
        self.advance(0.5)
        self.hw.value = 95
        self.advance(0.05)
        self.assertEqual(self.hw.value, 95)
        self.assertIsNone(self.c.brightness_fade)
        self.c.manual_until = 0
        self.c.tick()
        self.advance(0.5)
        self.assertLess(self.hw.value, 95)

    def test_brightening_finishes_in_one_second_and_dimming_in_three(self):
        self.hw.value = 20
        self.hw.lux = 10000
        self.c.command({'autoBrightness': True})
        self.c.tick()
        self.advance(1)
        self.assertEqual(self.hw.value, 100)
        self.assertIsNone(self.c.brightness_fade)
        self.hw.lux = 0
        self.c.tick()
        self.advance(1)
        self.assertGreater(self.hw.value, 18)
        self.advance(2)
        self.assertEqual(self.hw.value, 18)
        self.assertIsNone(self.c.brightness_fade)

    def test_dimming_uses_many_sub_percent_steps(self):
        self.hw.maximum = 400000
        self.hw.value = 200000
        self.hw.lux = 0
        self.c.command({'autoBrightness': True})
        self.c.tick()
        for _ in range(121): self.advance(module.FADE_INTERVAL)
        values = [value for kind, value in self.hw.writes if kind == 'brightness']
        self.assertGreater(len(values), 100)
        self.assertTrue(all(a > b for a, b in zip(values, values[1:])))
        self.assertLess(max(a - b for a, b in zip(values, values[1:])), 4000)
        self.assertEqual(self.hw.value, 72000)

    def test_brightening_interrupts_a_slow_dimming_fade(self):
        self.hw.value = 90
        self.hw.lux = 0
        self.c.command({'autoBrightness': True})
        self.c.tick()
        self.advance(1)
        self.assertGreater(self.hw.value, 18)
        self.hw.lux = 10000
        self.c.tick()
        self.advance(1)
        self.assertEqual(self.hw.value, 100)
        self.assertIsNone(self.c.brightness_fade)

    def test_disabling_cancels_an_in_progress_fade(self):
        self.c.command({'autoBrightness': True})
        self.c.tick()
        self.advance(0.5)
        self.c.command({'autoBrightness': False})
        self.advance(5)
        self.assertEqual(self.hw.value, 38)
        self.assertIsNone(self.c.brightness_fade)

    def test_idle_cancels_an_in_progress_fade(self):
        self.c.command({'autoBrightness': True})
        self.c.tick()
        self.advance(0.5)
        value = self.hw.value
        self.c.command({'idle': True})
        self.advance(5)
        self.assertEqual(self.hw.value, value)
        self.assertIsNone(self.c.brightness_fade)

    def test_cleanup_restores_but_preserves_enable_intent(self):
        self.c.command({'autoBrightness': True, 'autoColor': True})
        self.color_ticks(5)
        self.c.cleanup()
        self.assertEqual(self.hw.value, 38)
        self.assertEqual(self.hw.writes[-1], ('identity',))
        other = module.Controller(self.hw, self.c.path)
        self.assertTrue(other.state['autoBrightness'])
        self.assertTrue(other.state['autoColor'])
        self.assertIsNone(other.state['savedBrightness'])

    def test_disconnected_sensor_restores_after_repeated_failures(self):
        self.c.command({'autoBrightness': True, 'autoColor': True})
        self.c.tick()
        self.hw.failed = True
        for _ in range(5):
            with self.assertRaises(RuntimeError): self.c.tick()
        self.assertEqual(self.hw.value, 38)
        self.assertEqual(self.hw.temp, 6500)


if __name__ == '__main__': unittest.main()
