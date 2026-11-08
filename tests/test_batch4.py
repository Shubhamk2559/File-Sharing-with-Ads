"""Batch 4: admin can't switch off the last usable ad network; 'postback never arrives' is detected and reported."""
import asyncio
import copy
import unittest

import observability
from ads import service, settings


def _cfg(strict):
    settings.RUNTIME.update(monetag_postback=True, strict=strict)
    return settings.merged(None)


class LastNetworkGuard(unittest.TestCase):
    def tearDown(self):
        settings.RUNTIME.update(monetag_postback=False, strict=True)

    def test_cannot_disable_monetag_in_strict_mode(self):
        cur = _cfg(True)
        self.assertEqual(settings.servable_networks(cur), ["monetag"])
        new, errs = settings.validate_patch({"networks": {"monetag": {"enabled": False}}}, cur)
        self.assertTrue(errs)
        self.assertIn("last usable ad network", errs[0])
        self.assertIs(new, cur)                                   # nothing applied

    def test_can_disable_when_another_network_remains(self):
        cur = _cfg(False)
        cur["networks"]["tads"].update(enabled=True, allow_client_reward=True, unit_id="abc123")
        new, errs = settings.validate_patch({"networks": {"monetag": {"enabled": False}}}, cur)
        self.assertEqual(errs, [])
        self.assertFalse(new["networks"]["monetag"]["enabled"])

    def test_already_broken_config_can_still_be_fixed(self):
        cur = _cfg(True)
        cur["networks"]["monetag"]["enabled"] = False
        new, errs = settings.validate_patch({"networks": {"monetag": {"enabled": True}}}, cur)
        self.assertEqual(errs, [])
        self.assertTrue(new["networks"]["monetag"]["enabled"])


class PostbackMissingDetection(unittest.TestCase):
    def setUp(self):
        observability.METRICS.reset()
        self.alerts = []

        async def fake_alert(key, text, severity="warning"):
            self.alerts.append(key)
        self._orig = observability.ALERTS.alert
        observability.ALERTS.alert = fake_alert

    def tearDown(self):
        observability.ALERTS.alert = self._orig
        observability.METRICS.reset()

    def test_alert_once_after_three_unverified_with_no_postback_ever(self):
        for _ in range(5):
            asyncio.run(service._note_unverified())
        self.assertEqual(self.alerts, ["postback_never_received"])

    def test_no_alert_when_postbacks_do_arrive(self):
        observability.METRICS.inc("postback.accepted")
        for _ in range(5):
            asyncio.run(service._note_unverified())
        self.assertEqual(self.alerts, [])


    def test_manual_rejected_postback_does_not_hide_the_problem(self):
        observability.METRICS.inc("postback.rejected")      # e.g. owner opened the URL in a browser with literal {macros}
        for _ in range(3):
            asyncio.run(service._note_unverified())
        self.assertEqual(self.alerts, ["postback_never_received"])


if __name__ == "__main__":
    unittest.main()
