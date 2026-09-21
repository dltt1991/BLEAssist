import asyncio
import unittest

from ble_controller import ScanDevice, WheelchairBLE
from protocol import NOTIFY_UUID, SERVICE_UUID, WRITE_UUID, encode_joystick


class FakeServices:
    def get_service(self, uuid):
        return uuid if uuid == SERVICE_UUID else None

    def get_characteristic(self, uuid):
        return uuid if uuid in (NOTIFY_UUID, WRITE_UUID) else None


class FakeClient:
    def __init__(self, device, disconnected_callback=None, replies=None):
        self.device = device
        self.disconnected_callback = disconnected_callback
        self.replies = replies or {}
        self.services = FakeServices()
        self.is_connected = False
        self.notify_callback = None
        self.events = []
        self.writes = []
        self.write_delay = 0.0
        self.disconnect_count = 0

    async def connect(self):
        self.is_connected = True
        self.events.append("connect")

    async def start_notify(self, characteristic, callback):
        self.notify_callback = callback
        self.events.append(("notify", characteristic))

    async def write_gatt_char(self, characteristic, data, response):
        if not self.is_connected:
            raise RuntimeError("not connected")
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        record = (characteristic, bytes(data), response)
        self.writes.append(record)
        self.events.append(("write", bytes(data)))
        reply = self.replies.get(bytes(data).decode("ascii"))
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else None
        if reply and self.notify_callback:
            self.notify_callback(NOTIFY_UUID, bytearray(reply.encode("ascii")))

    async def disconnect(self):
        self.disconnect_count += 1
        self.is_connected = False
        self.events.append("disconnect")
        if self.disconnected_callback:
            self.disconnected_callback(self)


class WheelchairBLETests(unittest.IsolatedAsyncioTestCase):
    def make_controller(self, replies=None, timeout=0.05, interval=10.0):
        replies = replies if replies is not None else {
            "Y2AS": "Y1AS",
            "Y2BS": "Y1BS",
            "Y2O3S": "Y1O3S",
        }
        created = []

        def factory(device, disconnected_callback=None):
            client = FakeClient(device, disconnected_callback, replies)
            created.append(client)
            return client

        events = []
        controller = WheelchairBLE(
            lambda kind, payload=None: events.append((kind, payload)),
            client_factory=factory,
            exchange_timeout=timeout,
            send_interval=interval,
        )
        return controller, created, events

    async def connect(self, controller):
        device = ScanDevice("id", "chair", object(), True)
        await controller.connect(device)

    async def test_connect_subscribes_then_handshakes_in_order(self):
        controller, created, _ = self.make_controller()
        await self.connect(controller)
        client = created[0]

        self.assertTrue(controller.ready)
        self.assertEqual(controller.mode, 1)
        self.assertEqual(client.events[1], ("notify", NOTIFY_UUID))
        self.assertEqual(
            [write[1] for write in client.writes[:3]],
            [b"Y2AS", b"Y2BS", b"Y2O3S"],
        )
        self.assertTrue(all(write[2] is False for write in client.writes))
        await controller.disconnect(send_center=False)

    async def test_connect_timeout_identifies_exchange_without_duplicate_events(self):
        controller, created, events = self.make_controller(replies={})

        with self.assertRaisesRegex(TimeoutError, r"Y2AS.*Y1AS"):
            await self.connect(controller)

        self.assertFalse(controller.ready)
        self.assertEqual(created[0].disconnect_count, 1)
        self.assertEqual(
            [event for event in events if event == ("status", "disconnected")],
            [("status", "disconnected")],
        )
        self.assertEqual([event for event in events if event[0] == "error"], [])

    async def test_connect_retries_a_command_until_its_acknowledgement_arrives(self):
        replies = {
            "Y2AS": "Y1AS",
            "Y2BS": "Y1BS",
            "Y2O3S": [None, "Y1O3S"],
        }
        controller, created, _ = self.make_controller(replies=replies, timeout=0.1)
        controller._command_retry_interval = 0.01

        await self.connect(controller)

        writes = [item[1] for item in created[0].writes]
        self.assertEqual(writes.count(b"Y2O3S"), 2)
        await controller.disconnect(send_center=False)

    async def test_mode_changes_only_after_ack(self):
        replies = {
            "Y2AS": "Y1AS",
            "Y2BS": "Y1BS",
            "Y2O3S": "Y1O3S",
            "Y2O5S": "Y1O5S",
        }
        controller, _, events = self.make_controller(replies=replies)
        await self.connect(controller)
        await controller.set_mode(5)
        self.assertEqual(controller.mode, 5)
        confirmed_events = [event for event in events if event[0] == "mode"]
        self.assertEqual(confirmed_events, [("mode", 5)])

        with self.assertRaises(TimeoutError):
            await controller.set_mode(6)
        self.assertEqual(controller.mode, 5)
        confirmed_events = [event for event in events if event[0] == "mode"]
        self.assertEqual(confirmed_events, [("mode", 5), ("mode", 5)])
        await controller.disconnect(send_center=False)

    async def test_mode_change_pauses_motion_frames_until_acknowledged(self):
        replies = {
            "Y2AS": "Y1AS",
            "Y2BS": "Y1BS",
            "Y2O3S": "Y1O3S",
        }
        controller, created, _ = self.make_controller(replies=replies, interval=0.005)
        await self.connect(controller)
        client = created[0]
        before = len(client.writes)

        change = asyncio.create_task(controller.set_mode(1))
        await asyncio.sleep(0.01)

        pending = [item[1].decode("ascii") for item in client.writes[before:]]
        wait_index = pending.index("Y2WS")
        self.assertEqual(pending[wait_index : wait_index + 2], ["Y2WS", "Y2O1S"])
        self.assertFalse(any(frame.startswith("Y2C") for frame in pending[wait_index:]))

        client.notify_callback(NOTIFY_UUID, bytearray(b"Y1O1S"))
        await change
        await asyncio.sleep(0.01)

        frames = [item[1].decode("ascii") for item in client.writes[before:]]
        mode_index = frames.index("Y2O1S")
        self.assertTrue(any(frame.endswith("1S") for frame in frames[mode_index + 1 :]))
        await controller.disconnect(send_center=False)

    async def test_mode_change_uses_app_sequence_and_sends_command_once(self):
        replies = {
            "Y2AS": "Y1AS",
            "Y2BS": "Y1BS",
            "Y2O3S": "Y1O3S",
            "Y2O2S": "Y1O2S",
        }
        controller, created, _ = self.make_controller(replies=replies)
        await self.connect(controller)
        client = created[0]
        before = len(client.writes)

        await controller.set_mode(2)

        frames = [item[1].decode("ascii") for item in client.writes[before:]]
        self.assertEqual(frames[:2], ["Y2WS", "Y2O2S"])
        self.assertEqual(frames.count("Y2O2S"), 1)
        await controller.disconnect(send_center=False)

    async def test_mode_change_stops_immediately_on_ble_error(self):
        replies = {
            "Y2AS": "Y1AS",
            "Y2BS": "Y1BS",
            "Y2O3S": "Y1O3S",
            "Y2O2S": "Y1ES",
        }
        controller, created, _ = self.make_controller(replies=replies)
        await self.connect(controller)
        client = created[0]
        before = len(client.writes)

        with self.assertRaisesRegex(RuntimeError, "Y1ES"):
            await controller.set_mode(2)

        frames = [item[1].decode("ascii") for item in client.writes[before:]]
        self.assertEqual(frames.count("Y2O2S"), 1)
        self.assertEqual(controller.mode, 1)
        await controller.disconnect(send_center=False)

    async def test_late_ble_error_stops_motion_and_disconnects(self):
        controller, created, events = self.make_controller(interval=0.005)
        await self.connect(controller)
        client = created[0]
        await asyncio.sleep(0.01)

        client.notify_callback(NOTIFY_UUID, bytearray(b"Y1ES"))
        await asyncio.sleep(0.01)
        writes_after_error = len(client.writes)
        await asyncio.sleep(0.02)

        self.assertFalse(controller.ready)
        self.assertEqual(client.disconnect_count, 1)
        self.assertEqual(len(client.writes), writes_after_error)
        self.assertIn(
            ("error", "底盘返回 Y1ES（控制连接错误），已停止发送并断开"),
            events,
        )

    async def test_emergency_stop_writes_three_centers_and_disconnects(self):
        controller, created, _ = self.make_controller()
        await self.connect(controller)
        client = created[0]
        before = len(client.writes)

        await controller.emergency_stop()

        extra = client.writes[before:]
        self.assertEqual([item[1] for item in extra], [b"Y2C5005001S"] * 3)
        self.assertTrue(all(item[2] is False for item in extra))
        self.assertEqual(client.disconnect_count, 1)
        self.assertFalse(controller.ready)

    async def test_periodic_sender_uses_write_without_response(self):
        controller, created, _ = self.make_controller(interval=0.01)
        await self.connect(controller)
        client = created[0]
        controller.set_stick(1.0, 0.0, held=True)

        await asyncio.sleep(0.025)

        motion_writes = client.writes[3:]
        self.assertGreaterEqual(len(motion_writes), 2)
        expected = encode_joystick(1.0, 0.0, 1).encode("ascii")
        self.assertTrue(all(item == (WRITE_UUID, expected, False) for item in motion_writes))
        await controller.disconnect(send_center=False)

    async def test_sender_immediately_reports_a_changed_motion_frame(self):
        controller, _, events = self.make_controller(interval=0.01)
        await self.connect(controller)
        controller.set_stick(1.0, 0.0, held=True)

        await asyncio.sleep(0.025)

        expected = encode_joystick(1.0, 0.0, 1)
        motion_events = [payload for kind, payload in events if kind == "motion"]
        self.assertEqual(motion_events.count(expected), 1)
        await controller.disconnect(send_center=False)

    async def test_sender_periodically_reports_an_unchanged_motion_frame(self):
        controller, _, events = self.make_controller(interval=0.005)
        await self.connect(controller)
        controller.set_stick(1.0, 0.0, held=True)

        expected = encode_joystick(1.0, 0.0, 1)
        for _ in range(100):
            motion_events = [payload for kind, payload in events if kind == "motion"]
            if motion_events.count(expected) >= 2:
                break
            await asyncio.sleep(0.005)
        self.assertGreaterEqual(motion_events.count(expected), 2)
        await controller.disconnect(send_center=False)

    async def test_sender_interval_does_not_add_ble_write_latency(self):
        controller, created, _ = self.make_controller(interval=0.02)
        await self.connect(controller)
        client = created[0]
        client.write_delay = 0.01
        before = len(client.writes)

        await asyncio.sleep(0.085)

        self.assertGreaterEqual(len(client.writes) - before, 4)
        await controller.disconnect(send_center=False)

    async def test_can_reconnect_after_remote_disconnect(self):
        controller, created, _ = self.make_controller()
        await self.connect(controller)
        first_client = created[0]
        first_client.is_connected = False
        first_client.disconnected_callback(first_client)

        await self.connect(controller)

        self.assertEqual(len(created), 2)
        self.assertTrue(controller.ready)
        await controller.disconnect(send_center=False)


if __name__ == "__main__":
    unittest.main()
