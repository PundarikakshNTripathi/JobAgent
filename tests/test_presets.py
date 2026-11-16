"""Routing presets: the shipped file is complete, applying one rewrites routing and fallbacks, and the current preset is recognised."""
import unittest
import _env  # noqa: F401  (isolated workspace)
import common, presets


class Presets(unittest.TestCase):
    def test_presets_cover_every_task(self):
        ps = presets.load()
        self.assertIn("recommended", ps)
        kinds = set(ps["recommended"]["routing"])
        for name, p in ps.items():
            self.assertTrue(kinds <= set(p["routing"]), f"{name} misses {kinds - set(p['routing'])}")
            self.assertTrue(p["fallbacks"])

    def test_low_cost_never_uses_claude(self):
        p = presets.load()["low-cost"]
        self.assertNotIn("claude", p["fallbacks"])
        self.assertFalse([k for k, v in p["routing"].items() if v["provider"] in ("claude", "auto")])

    def test_apply_and_recognise(self):
        presets.apply("low-cost")
        self.assertEqual(presets.current(), "low-cost")
        self.assertEqual(common.SETTINGS["llm"]["fallbacks"], ["agy", "local"])
        common.SETTINGS["llm"]["routing"]["chat"] = {"provider": "local", "model": "local", "effort": ""}
        self.assertEqual(presets.current(), "custom")
        presets.apply("recommended")
        self.assertEqual(presets.current(), "recommended")
        with self.assertRaises(ValueError):
            presets.apply("nope")


if __name__ == "__main__":
    unittest.main()
