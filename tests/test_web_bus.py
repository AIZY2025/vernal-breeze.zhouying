import threading
import time
import unittest

import _support  # noqa: F401

from core.orchestrator import SandboxCancelled
from web.server import AnswerTimeout, EventBus, NotAwaiting, WebUI


class EventBusTest(unittest.TestCase):
    def test_emit_and_read(self):
        bus = EventBus()
        bus.emit("info", text="a")
        events = bus.read(0, timeout=0.01)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], 1)
        bus.emit("info", text="b")
        events = bus.read(1, timeout=0.01)
        self.assertEqual([event["text"] for event in events], ["b"])

    def test_since_ahead_of_stream_is_clamped(self):
        bus = EventBus()
        bus.emit("info", text="a")
        self.assertEqual(bus.read(99, timeout=0.01), [])

    def test_trim_keeps_latest(self):
        bus = EventBus()
        total = EventBus.MAX_EVENTS + 5
        for index in range(total):
            bus.emit("info", text=str(index))
        events = bus.read(0, timeout=0.01)
        self.assertEqual(len(events), EventBus.MAX_EVENTS)
        self.assertEqual(events[-1]["text"], str(total - 1))
        self.assertEqual(bus.history(), total)

    def test_reset_clears(self):
        bus = EventBus()
        bus.emit("info", text="a")
        bus.reset()
        self.assertEqual(bus.history(), 0)
        self.assertEqual(bus.read(0, timeout=0.01), [])

    def test_reset_clears_awaiting_state(self):
        bus = EventBus()
        bus.ask("要不要继续")
        self.assertTrue(bus.awaiting())
        bus.reset()
        self.assertFalse(bus.awaiting())
        with self.assertRaises(NotAwaiting):
            bus.answer("回答")

    def test_ask_then_answer_is_consumed_once(self):
        bus = EventBus()
        bus.ask("要不要继续")
        self.assertTrue(bus.awaiting())
        bus.answer("继续")
        self.assertFalse(bus.awaiting())
        events = bus.read(0, timeout=0.01)
        self.assertEqual([(ev["type"], ev.get("text")) for ev in events],
                         [("ask", None), ("user", "继续")])
        self.assertEqual(bus.wait_answer(0.01), "继续")
        with self.assertRaises(NotAwaiting):
            bus.answer("再来一次")

    def test_answer_without_ask_is_rejected_and_not_queued(self):
        bus = EventBus()
        with self.assertRaises(NotAwaiting) as ctx:
            bus.answer("没轮到就作答")
        self.assertIn("已忽略", str(ctx.exception))
        self.assertEqual([ev for ev in bus.read(0, timeout=0.01) if ev["type"] == "user"], [])
        with self.assertRaises(AnswerTimeout):
            bus.wait_answer(0.01)

    def test_wait_answer_timeout_resets_awaiting(self):
        bus = EventBus()
        bus.ask("要不要继续")
        with self.assertRaises(AnswerTimeout):
            bus.wait_answer(0.01)
        self.assertFalse(bus.awaiting())
        with self.assertRaises(NotAwaiting):
            bus.answer("迟到的回答")

    def test_webui_ask_waits_on_bus_awaiting(self):
        bus = EventBus()
        ui = WebUI(bus)

        def reply():
            deadline = time.time() + 2
            while time.time() < deadline and not bus.awaiting():
                time.sleep(0.01)
            bus.answer("收到")

        worker = threading.Thread(target=reply, daemon=True)
        worker.start()
        self.assertEqual(ui.ask("要不要继续", echo=False), "收到")
        worker.join(timeout=2)
        asks = [ev for ev in bus.read(0, timeout=0.01) if ev["type"] == "ask"]
        self.assertEqual(len(asks), 1)
        self.assertFalse(asks[0]["echo"])

    def test_wake_releases_blocked_answer(self):
        bus = EventBus()
        bus.ask("要不要继续")
        bus.answer("还没轮到就作答")
        bus.wake()
        with self.assertRaises(SandboxCancelled):
            bus.wait_answer(0.1)


if __name__ == "__main__":
    unittest.main()