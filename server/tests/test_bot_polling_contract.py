from __future__ import annotations

import unittest
from pathlib import Path


class BotPollingContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bot_text = Path("/root/kakao_bot/bot.txt").read_text(encoding="utf-8")

    def test_bot_script_keeps_polling_endpoints_and_status_strings(self) -> None:
        self.assertIn("/polling/pull", self.bot_text)
        self.assertIn("/polling/ack", self.bot_text)
        self.assertIn("activeTransport: polling_outbox", self.bot_text)
        self.assertIn("deliveryMode: polling_outbox", self.bot_text)
        self.assertIn("mode: polling_outbox", self.bot_text)

    def test_bot_script_uses_kakao_package_name_alias(self) -> None:
        self.assertIn("KAKAO_PACKAGE_NAME", self.bot_text)
        self.assertIn("SOCKET_PACKAGE_NAME", self.bot_text)
        self.assertIn("getKakaoPackageName()", self.bot_text)

    def test_bot_script_does_not_claim_socket_delivery_is_active(self) -> None:
        self.assertNotIn("socket push is active", self.bot_text)
        self.assertNotIn("deliveryMode: socket", self.bot_text)

    def test_bot_script_keeps_polling_item_diagnostics(self) -> None:
        self.assertIn("function buildPollingItemDebugInfo(item, messages)", self.bot_text)
        self.assertIn("polling item exception trigger=", self.bot_text)
        self.assertIn("json_parse_error=", self.bot_text)


if __name__ == "__main__":
    unittest.main()
