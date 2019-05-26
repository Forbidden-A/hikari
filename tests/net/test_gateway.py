#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio
import json
import math
import urllib.parse as urlparse
import zlib

import async_timeout
import asynctest

from hikari.net import gateway
from tests import _helpers


def teardown_function():
    _helpers.purge_loop()


class MockGateway(gateway.GatewayConnection):
    def __init__(self, **kwargs):
        gateway.GatewayConnection.__init__(self, **kwargs)

        self.ws = asynctest.MagicMock()
        self.ws.close = asynctest.CoroutineMock()
        self.ws.send = asynctest.CoroutineMock()
        self.ws.recv = asynctest.CoroutineMock()
        self.ws.wait_closed = self._wait_closed

    @staticmethod
    async def _wait_closed():
        await asyncio.sleep(0.1)


@_helpers.non_zombified_async_test()
async def test_init_produces_valid_url(event_loop):
    """GatewayConnection.__init__ should produce a valid query fragment for the URL."""
    gw = gateway.GatewayConnection(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234")
    bits: urlparse.ParseResult = urlparse.urlparse(gw.uri)

    assert bits.scheme == "wss"
    assert bits.hostname == "gateway.discord.gg"
    assert bits.port == 4949
    assert bits.query == "v=7&encoding=json&compression=zlib-stream"
    assert not bits.fragment


@_helpers.non_zombified_async_test()
async def test_do_resume_triggers_correct_signals(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234")

    try:
        await gw._trigger_resume(69, "boom")
        assert False, "No exception raised"
    except gateway.ResumeConnection:
        gw.ws.close.assert_awaited_once_with(code=69, reason="boom")


@_helpers.non_zombified_async_test()
async def test_do_reidentify_triggers_correct_signals(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234")

    try:
        await gw._trigger_reidentify(69, "boom")
        assert False, "No exception raised"
    except gateway.RestartConnection:
        gw.ws.close.assert_awaited_once_with(code=69, reason="boom")


@_helpers.non_zombified_async_test()
async def test_send_json_calls_websocket(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw._logger = asynctest.MagicMock()

    # pretend to sleep only
    with asynctest.patch("asyncio.sleep", new=asyncio.coroutine(lambda _: None)):
        await gw._send_json({})

    gw.ws.send.assert_awaited_once_with("{}")

    # Ensure we didn't get ratelimited on a single call.
    gw._logger.debug.assert_not_called()


@_helpers.non_zombified_async_test()
async def test_send_json_eventually_gets_ratelimited(event_loop):
    MockGateway._COOLDOWN = float("inf")
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)

    futures = []

    for i in range(130):
        futures.append(asyncio.create_task(gw._send_json({})))

    try:
        await gw._rate_limit._full_event.wait()
    finally:
        [f.cancel() for f in futures]


@_helpers.non_zombified_async_test()
async def test_send_json_wont_send_massive_payloads(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw._handle_payload_oversize = asynctest.MagicMock(wraps=gw._handle_payload_oversize)
    pl = {"d": {"lolololilolo": "lol" * 4096}, "op": "1", "blah": 2}
    await gw._send_json(pl)
    gw._handle_payload_oversize.assert_called_once_with(pl)


@_helpers.non_zombified_async_test()
async def test_receive_json_calls_recv_at_least_once(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw.ws.recv = asynctest.CoroutineMock(return_value="{}")
    await gw._recv_json()
    gw.ws.recv.assert_any_call()


@_helpers.non_zombified_async_test()
async def test_receive_json_closes_connection_if_payload_was_not_a_dict(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw._trigger_reidentify = asynctest.CoroutineMock()
    gw.ws.recv = asynctest.CoroutineMock(return_value="[]")
    await gw._recv_json()
    gw.ws.recv.assert_any_call()
    gw._trigger_reidentify.assert_awaited_once_with(code=gateway.ClientExit.TYPE_ERROR, reason="Expected JSON object.")


@_helpers.non_zombified_async_test()
async def test_receive_json_when_receiving_string_decodes_immediately(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)

    with asynctest.patch("json.loads", new=asynctest.MagicMock(return_value={})):
        recv_value = "{" '  "foo": "bar",' '  "baz": "bork",' '  "qux": ["q", "u", "x", "x"]' "}"
        gw.ws.recv = asynctest.CoroutineMock(return_value=recv_value)
        await gw._recv_json()
        # noinspection PyUnresolvedReferences,PyUnresolvedReferences
        json.loads.assert_called_with(recv_value)


@_helpers.non_zombified_async_test()
async def test_receive_json_when_receiving_zlib_payloads_collects_before_decoding(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)

    with asynctest.patch("json.loads", new=asynctest.MagicMock(return_value={})):
        recv_value = "{" '  "foo": "bar",' '  "baz": "bork",' '  "qux": ["q", "u", "x", "x"]' "}".encode("utf-8")

        payload = zlib.compress(recv_value) + b"\x00\x00\xff\xff"

        chunk_size = 16
        chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]

        gw.ws.recv = asynctest.CoroutineMock(side_effect=chunks)
        await gw._recv_json()
        # noinspection PyUnresolvedReferences,PyUnresolvedReferences
        json.loads.assert_called_with(recv_value.decode("utf-8"))


@_helpers.non_zombified_async_test()
async def test_small_zlib_payloads_leave_buffer_alone(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)

    with asynctest.patch("json.loads", new=asynctest.MagicMock(return_value={})):
        recv_value = "{" '  "foo": "bar",' '  "baz": "bork",' '  "qux": ["q", "u", "x", "x"]' "}".encode("utf-8")

        payload = zlib.compress(recv_value) + b"\x00\x00\xff\xff"

        chunk_size = 16
        chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]

        first_array = gw._in_buffer
        gw.ws.recv = asynctest.CoroutineMock(side_effect=chunks)
        await gw._recv_json()
        assert gw._in_buffer is first_array


@_helpers.non_zombified_async_test()
async def test_massive_zlib_payloads_cause_buffer_replacement(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)

    with asynctest.patch("json.loads", new=asynctest.MagicMock(return_value={})):
        recv_value = "{" '  "foo": "bar",' '  "baz": "bork",' '  "qux": ["q", "u", "x", "x"]' "}".encode("utf-8")

        payload = zlib.compress(recv_value) + b"\x00\x00\xff\xff"

        chunk_size = 16
        chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]

        first_array = gw._in_buffer
        gw.max_persistent_buffer_size = 3
        gw.ws.recv = asynctest.CoroutineMock(side_effect=chunks)
        await gw._recv_json()
        assert gw._in_buffer is not first_array


@_helpers.non_zombified_async_test()
async def test_heartbeat_beats_at_interval(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw._heartbeat_interval = 0.01

    task = asyncio.create_task(gw._keep_alive())
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        gw.ws.send.assert_awaited_with('{"op": 1, "d": null}')


@_helpers.non_zombified_async_test()
async def test_heartbeat_shuts_down_when_closure_request(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw._heartbeat_interval = 0.01

    with async_timeout.timeout(5):
        task = asyncio.create_task(gw._keep_alive())
        try:
            await asyncio.sleep(0.1)
        finally:
            await gw.close(True)
            await task


@_helpers.non_zombified_async_test()
async def test_heartbeat_if_not_acknowledged_in_time_closes_connection_with_resume(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw._last_heartbeat_sent = -float("inf")
    gw._heartbeat_interval = 0

    task = asyncio.create_task(gw._keep_alive())

    with async_timeout.timeout(0.1):
        await asyncio.sleep(0)

    task.cancel()
    gw.ws.close.assert_awaited_once()


@_helpers.non_zombified_async_test()
async def test_slow_loop_produces_warning(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw._heartbeat_interval = 0
    gw.heartbeat_latency = 0
    gw._logger = asynctest.MagicMock()
    task = asyncio.create_task(gw._keep_alive())

    with async_timeout.timeout(1):
        await asyncio.sleep(0.1)

    task.cancel()
    gw._logger.warning.assert_called_once()


@_helpers.non_zombified_async_test()
async def test_send_heartbeat(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw._send_json = asynctest.CoroutineMock()
    await gw._send_heartbeat()
    gw._send_json.assert_called_with({"op": 1, "d": None})


@_helpers.non_zombified_async_test()
async def test_send_ack(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw._send_json = asynctest.CoroutineMock()
    await gw._send_ack()
    gw._send_json.assert_called_with({"op": 11})


@_helpers.non_zombified_async_test()
async def test_handle_ack(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw._last_heartbeat_sent = 0
    await gw._handle_ack()
    assert not math.isnan(gw.heartbeat_latency) and not math.isinf(gw.heartbeat_latency)


@_helpers.non_zombified_async_test()
async def test_recv_hello_when_is_hello(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw._recv_json = asynctest.CoroutineMock(
        return_value={"op": 10, "d": {"heartbeat_interval": 12345, "_trace": ["foo"]}}
    )
    await gw._recv_hello()

    assert gw.trace == ["foo"]
    assert gw._heartbeat_interval == 12.345


@_helpers.non_zombified_async_test()
async def test_recv_hello_when_is_not_hello_causes_resume(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw._recv_json = asynctest.CoroutineMock(
        return_value={"op": 9, "d": {"heartbeat_interval": 12345, "_trace": ["foo"]}}
    )

    gw._trigger_resume = asynctest.CoroutineMock()
    await gw._recv_hello()
    gw._trigger_resume.assert_awaited()


@_helpers.non_zombified_async_test()
async def test_send_resume(event_loop):
    gw = MockGateway(host="wss://gateway.discord.gg:4949/", loop=event_loop, token="1234", shard_id=None)
    gw._session_id = 1234321
    gw._seq = 69_420
    gw._send_json = asynctest.CoroutineMock()
    await gw._send_resume()
    gw._send_json.assert_called_with({"op": 6, "d": {"token": "1234", "session_id": 1234321, "seq": 69_420}})


@_helpers.non_zombified_async_test()
async def test_send_identify_when_not_redacted_default_behaviour(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=None,
        large_threshold=69,
        incognito=False,
    )
    gw._session_id = 1234321
    gw._seq = 69_420
    gw._send_json = asynctest.CoroutineMock()

    with asynctest.patch("hikari.net.gateway.python_version", new=lambda: "python3"), asynctest.patch(
        "hikari.net.gateway.library_version", new=lambda: "vx.y.z"
    ), asynctest.patch("platform.system", new=lambda: "leenuks"):
        await gw._send_identify()
        gw._send_json.assert_called_with(
            {
                "op": 2,
                "d": {
                    "token": "1234",
                    "compress": False,
                    "large_threshold": 69,
                    "properties": {"$os": "leenuks", "$browser": "vx.y.z", "$device": "python3"},
                },
            }
        )


@_helpers.non_zombified_async_test()
async def test_send_identify_when_redacted(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=None,
        large_threshold=69,
        incognito=True,
    )
    gw._session_id = 1234321
    gw._seq = 69_420
    gw._send_json = asynctest.CoroutineMock()

    with asynctest.patch("hikari.net.gateway.python_version", new=lambda: "python3"), asynctest.patch(
        "hikari.net.gateway.library_version", new=lambda: "vx.y.z"
    ), asynctest.patch("platform.system", new=lambda: "leenuks"):
        await gw._send_identify()
        gw._send_json.assert_called_with(
            {
                "op": 2,
                "d": {
                    "token": "1234",
                    "compress": False,
                    "large_threshold": 69,
                    "properties": {"$os": "os", "$browser": "browser", "$device": "device"},
                },
            }
        )


@_helpers.non_zombified_async_test()
async def test_send_identify_includes_sharding_info_if_present(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    gw._send_json = asynctest.CoroutineMock()

    await gw._send_identify()
    payload = gw._send_json.call_args[0][0]

    assert "d" in payload
    assert "shard" in payload["d"]
    assert payload["d"]["shard"] == [917, 1234]


@_helpers.non_zombified_async_test()
async def test_send_identify_includes_status_info_if_present(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
        initial_presence={"foo": "bar"},
    )
    gw._send_json = asynctest.CoroutineMock()

    await gw._send_identify()
    payload = gw._send_json.call_args[0][0]

    assert "d" in payload
    assert "status" in payload["d"]
    assert payload["d"]["status"] == {"foo": "bar"}


@_helpers.non_zombified_async_test()
async def test_process_events_halts_if_closed_event_is_set(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    gw._process_one_event = asynctest.CoroutineMock()
    gw.closed_event.set()
    await gw._process_events()
    gw._process_one_event.assert_not_awaited()


@_helpers.non_zombified_async_test()
async def test_process_one_event_updates_seq_if_provided_from_payload(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    gw._recv_json = asynctest.CoroutineMock(return_value={"op": 0, "d": {}, "s": 69})
    await gw._process_one_event()
    assert gw._seq == 69


@_helpers.non_zombified_async_test()
async def test_process_events_does_not_update_seq_if_not_provided_from_payload(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )

    gw._seq = 123
    gw._recv_json = asynctest.CoroutineMock(return_value={"op": 0, "d": {}})
    await gw._process_one_event()
    assert gw._seq == 123


@_helpers.non_zombified_async_test()
async def test_process_events_on_dispatch_opcode(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )

    async def flag_death_on_call():
        gw.closed_event.set()
        return {"op": 0, "d": {}, "t": "explosion"}

    gw._recv_json = flag_death_on_call

    gw.dispatch = asynctest.CoroutineMock()
    await gw._process_one_event()
    gw.dispatch.assert_awaited_with("explosion", {})


@_helpers.non_zombified_async_test()
async def test_process_events_on_heartbeat_opcode(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )

    async def flag_death_on_call():
        gw.closed_event.set()
        return {"op": 1, "d": {}}

    gw._recv_json = flag_death_on_call

    gw._send_ack = asynctest.CoroutineMock()
    await gw._process_one_event()
    gw._send_ack.assert_any_await()


@_helpers.non_zombified_async_test()
async def test_process_events_on_heartbeat_ack_opcode(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )

    async def flag_death_on_call():
        gw.closed_event.set()
        return {"op": 11, "d": {}}

    gw._recv_json = flag_death_on_call

    gw._handle_ack = asynctest.CoroutineMock()
    await gw._process_one_event()
    gw._handle_ack.assert_any_await()


@_helpers.non_zombified_async_test()
async def test_process_events_on_reconnect_opcode(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )

    async def flag_death_on_call():
        gw.closed_event.set()
        return {"op": 7, "d": {}}

    gw._recv_json = flag_death_on_call

    try:
        await gw._process_one_event()
        assert False, "No error raised"
    except gateway.RestartConnection:
        pass


@_helpers.non_zombified_async_test()
async def test_process_events_on_resumable_invalid_session_opcode(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )

    async def flag_death_on_call():
        gw.closed_event.set()
        return {"op": 9, "d": True}

    gw._recv_json = flag_death_on_call

    try:
        await gw._process_one_event()
        assert False, "No error raised"
    except gateway.ResumeConnection:
        pass


@_helpers.non_zombified_async_test()
async def test_process_events_on_non_resumable_invalid_session_opcode(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )

    async def flag_death_on_call():
        gw.closed_event.set()
        return {"op": 9, "d": False}

    gw._recv_json = flag_death_on_call

    try:
        await gw._process_one_event()
        assert False, "No error raised"
    except gateway.RestartConnection:
        pass


@_helpers.non_zombified_async_test()
async def test_process_events_on_unrecognised_opcode_passes_silently(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )

    async def flag_death_on_call():
        gw.closed_event.set()
        return {"op": -1, "d": False}

    gw._recv_json = flag_death_on_call
    await gw._process_one_event()


@_helpers.non_zombified_async_test()
async def test_request_guild_members(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    gw._send_json = asynctest.CoroutineMock()
    await gw.request_guild_members(1234)
    gw._send_json.assert_called_with({"op": 8, "d": {"guild_id": "1234", "query": "", "limit": 0}})


@_helpers.non_zombified_async_test()
async def test_update_status(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    gw._send_json = asynctest.CoroutineMock()
    await gw.update_status(1234, {"name": "boom"}, "dead", True)
    gw._send_json.assert_called_with(
        {"op": 3, "d": {"idle": 1234, "game": {"name": "boom"}, "status": "dead", "afk": True}}
    )


@_helpers.non_zombified_async_test()
async def test_update_voice_state(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    gw._send_json = asynctest.CoroutineMock()
    await gw.update_voice_state(1234, 5678, False, True)
    gw._send_json.assert_called_with(
        {"op": 4, "d": {"guild_id": "1234", "channel_id": "5678", "self_mute": False, "self_deaf": True}}
    )


@_helpers.non_zombified_async_test()
async def test_no_blocking_close(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    gw.ws.wait_closed = asynctest.CoroutineMock()
    await gw.close(False)
    assert gw.closed_event.is_set()
    gw.ws.wait_closed.assert_not_awaited()


@_helpers.non_zombified_async_test()
async def test_blocking_close(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    gw.ws.wait_closed = asynctest.CoroutineMock()
    await gw.close(True)
    assert gw.closed_event.is_set()
    gw.ws.wait_closed.assert_awaited()


@_helpers.non_zombified_async_test()
async def test_shut_down_run_does_not_loop(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    gw._recv_json = asynctest.CoroutineMock()
    gw.closed_event.set()
    await gw.run()


@_helpers.non_zombified_async_test()
async def test_invalid_session_when_cannot_resume_does_not_resume(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    gw._trigger_resume = asynctest.CoroutineMock(wraps=gw._trigger_resume)
    gw._trigger_reidentify = asynctest.CoroutineMock(wraps=gw._trigger_reidentify)
    pl = {"op": gateway.Opcode.INVALID_SESSION.value, "d": False}
    gw._recv_json = asynctest.CoroutineMock(return_value=pl)

    try:
        await gw._process_one_event()
        assert False, "No exception raised"
    except gateway.RestartConnection:
        pass

    gw._trigger_reidentify.assert_awaited_once()
    gw._trigger_resume.assert_not_awaited()


@_helpers.non_zombified_async_test()
async def test_invalid_session_when_can_resume_does_resume(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    gw._trigger_resume = asynctest.CoroutineMock(wraps=gw._trigger_resume)
    gw._trigger_reidentify = asynctest.CoroutineMock(wraps=gw._trigger_reidentify)
    pl = {"op": gateway.Opcode.INVALID_SESSION.value, "d": True}
    gw._recv_json = asynctest.CoroutineMock(return_value=pl)

    try:
        await gw._process_one_event()
        assert False, "No exception raised"
    except gateway.ResumeConnection:
        pass

    gw._trigger_resume.assert_awaited_once()
    gw._trigger_reidentify.assert_not_awaited()


@_helpers.non_zombified_async_test()
async def test_dispatch_ready(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    #  *sweats furiously*
    pl = {
        "op": 0,
        "t": "READY",
        "d": {
            # https://discordapp.com/developers/docs/topics/gateway#ready-ready-event-fields
            "v": 69,
            "_trace": ["potato.com", "tomato.net"],
            "shard": [9, 18],
            "session_id": "69420lmaolmao",
            "guilds": [{"id": "9182736455463", "unavailable": True}, {"id": "72819099110270", "unavailable": True}],
            "private_channels": [],  # always empty /shrug
            "user": {
                "id": "81624",
                "username": "Ben_Dover",
                "discriminator": 9921,
                "avatar": "a_d41d8cd98f00b204e9800998ecf8427e",
                "bot": bool("of course i am"),
                "mfa_enabled": True,
                "locale": "en_gb",
                "verified": False,
                "email": "chestylaroo@boing.biz",
                "flags": 69,
                "premimum_type": 0,
            },
        },
    }
    gw._recv_json = asynctest.CoroutineMock(return_value=pl)
    gw._handle_ready = asynctest.CoroutineMock()
    await gw._process_one_event()
    gw._handle_ready.assert_awaited_once_with(pl["d"])


@_helpers.non_zombified_async_test()
async def test_dispatch_resume(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    pl = {"op": 0, "t": "RESUMED", "d": {"_trace": ["potato.com", "tomato.net"]}}

    gw._recv_json = asynctest.CoroutineMock(return_value=pl)
    gw._handle_resumed = asynctest.CoroutineMock()
    await gw._process_one_event()
    gw._handle_resumed.assert_awaited_once_with(pl["d"])


@_helpers.non_zombified_async_test()
async def test_handle_ready(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    #  *sweats furiously*
    pl = {
        "op": 0,
        "t": "READY",
        "d": {
            # https://discordapp.com/developers/docs/topics/gateway#ready-ready-event-fields
            "v": 69,
            "_trace": ["potato.com", "tomato.net"],
            "shard": [9, 18],
            "session_id": "69420lmaolmao",
            "guilds": [{"id": "9182736455463", "unavailable": True}, {"id": "72819099110270", "unavailable": True}],
            "private_channels": [],  # always empty /shrug
            "user": {
                "id": "81624",
                "username": "Ben_Dover",
                "discriminator": 9921,
                "avatar": "a_d41d8cd98f00b204e9800998ecf8427e",
                "bot": bool("of course i am"),
                "mfa_enabled": True,
                "locale": "en_gb",
                "verified": False,
                "email": "chestylaroo@boing.biz",
                "flags": 69,
                "premimum_type": 0,
            },
        },
    }
    await gw._handle_ready(pl["d"])


@_helpers.non_zombified_async_test()
async def test_handle_resume(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )
    pl = {"op": 0, "t": "RESUMED", "d": {"_trace": ["potato.com", "tomato.net"]}}
    await gw._handle_resumed(pl["d"])


@_helpers.non_zombified_async_test()
async def test_process_events_calls_process_one_event(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )

    gw._process_one_event = asynctest.CoroutineMock(
        side_effect=asyncio.coroutine(lambda *_, **__: gw.closed_event.set())
    )
    await gw._process_events()
    gw._process_one_event.assert_awaited_once()


@_helpers.non_zombified_async_test()
async def test_run_calls_run_once(event_loop):
    gw = MockGateway(
        host="wss://gateway.discord.gg:4949/",
        loop=event_loop,
        token="1234",
        shard_id=917,
        shard_count=1234,
        large_threshold=69,
        incognito=False,
    )

    gw.run_once = asynctest.CoroutineMock(side_effect=asyncio.coroutine(lambda *_, **__: gw.closed_event.set()))
    await gw.run()
    gw.run_once.assert_awaited_once()


def mock_run_once_parts(timeout=10):
    def decorator(coro):
        async def wrapper(event_loop):
            class CoroMock(asynctest.CoroutineMock):
                def __await__(self):
                    gw._logger.info("%s awaited", self)
                    yield from super().__await__()

            class DummyWebsocketsConnector(asynctest.MagicMock):
                __aenter__ = asynctest.CoroutineMock(side_effect=lambda s: s)

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

                recv = CoroMock()
                send = CoroMock()

            gw = MockGateway(
                host="wss://gateway.discord.gg:4949/",
                loop=event_loop,
                token="1234",
                shard_id=917,
                shard_count=1234,
                large_threshold=69,
                incognito=False,
            )
            gw._recv_hello = CoroMock()
            gw._send_resume = CoroMock()
            gw._send_identify = CoroMock()
            gw._keep_alive = CoroMock()
            gw._process_events = CoroMock()
            with asynctest.patch("asyncio.sleep", new=asyncio.coroutine(lambda t: None)), asynctest.patch(
                "websockets.connect", new=DummyWebsocketsConnector
            ):
                return await coro(event_loop, gw, DummyWebsocketsConnector)

        wrapper.__name__ = coro.__name__
        wrapper.__qualname__ = coro.__qualname__
        return _helpers.non_zombified_async_test(timeout=timeout)(wrapper)

    return decorator


@mock_run_once_parts()
async def test_run_once_opens_connection(_, gw, connect):
    await gw.run_once()
    assert connect.__aenter__.awaited_once()


@mock_run_once_parts()
async def test_run_once_waits_for_hello(_, gw, __):
    await gw.run_once()
    gw._recv_hello.assert_awaited_once()


@mock_run_once_parts()
async def test_run_once_identifies_normally(_, gw, __):
    await gw.run_once()
    gw._send_identify.assert_awaited_once()
    gw._send_resume.assert_not_awaited()


@mock_run_once_parts()
async def test_run_once_resumes_when_seq_and_session_id_set(_, gw, __):
    gw._seq = 59
    gw._session_id = 1234
    await gw.run_once()
    gw._send_resume.assert_awaited_once()
    gw._send_identify.assert_not_awaited()


@mock_run_once_parts()
async def test_run_once_spins_up_heartbeat_keep_alive_task(_, gw, __):
    await gw.run_once()
    gw._keep_alive.assert_awaited()


@mock_run_once_parts()
async def test_run_once_spins_up_event_processing_task(_, gw, __):
    await gw.run_once()
    gw._process_events.assert_awaited()


@mock_run_once_parts()
async def test_run_once_never_reconnect_is_raised_via_RestartConnection(_, gw, __):
    gw._process_events = asynctest.CoroutineMock(
        side_effect=gateway.RestartConnection(gw._NEVER_RECONNECT_CODES[0], "some lazy message")
    )
    try:
        await gw.run_once()
        assert False, "no runtime error raised"
    except RuntimeError as ex:
        assert isinstance(ex.__cause__, gateway.RestartConnection)


@mock_run_once_parts()
async def test_run_once_never_reconnect_is_raised_via_ResumeConnection(_, gw, __):
    gw._process_events = asynctest.CoroutineMock(
        side_effect=gateway.ResumeConnection(gw._NEVER_RECONNECT_CODES[0], "some lazy message")
    )
    try:
        await gw.run_once()
        assert False, "no runtime error raised"
    except RuntimeError as ex:
        assert isinstance(ex.__cause__, gateway.ResumeConnection)


@mock_run_once_parts()
async def test_run_once_RestartConnection(_, gw, __):
    gw._process_events = asynctest.CoroutineMock(
        side_effect=gateway.RestartConnection(gateway.ServerExit.INVALID_SEQ, "some lazy message")
    )
    start = 1, 2, ["foo"]
    gw._seq, gw._session_id, gw.trace = start
    await gw.run_once()
    assert gw._seq is None, "seq was not reset"
    assert gw._session_id is None, "session_id was not reset"
    assert gw.trace == [], "trace was not cleared"


@mock_run_once_parts()
async def test_run_once_ResumeConnection(_, gw, __):
    gw._process_events = asynctest.CoroutineMock(
        side_effect=gateway.ResumeConnection(gateway.ServerExit.RATE_LIMITED, "some lazy message")
    )
    start = 1, 2, ["foo"]
    gw._seq, gw._session_id, gw.trace = start
    await gw.run_once()
    assert gw._seq == 1, "seq was reset"
    assert gw._session_id == 2, "session_id was reset"
    assert gw.trace == ["foo"], "trace was cleared"
