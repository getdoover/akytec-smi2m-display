"""Application logic tests against a fake Modbus bus.

These exercise the parts that decide *what* goes on the wire — the desired vs
written diff, the blank timeout, and the RPC surface — without needing a device
agent or a real display. The app object is built without running pydoover's
``__init__`` and its collaborators are injected, which keeps the tests on this
app's own logic rather than on framework wiring.
"""

import time

import pytest

from smi2m_display.application import SMI2MApplication
from smi2m_display.smi2m_driver import (
    DISPLAY_BLOCK_START,
    REG_STRING_LENGTH,
    REG_VALUE_IMAGE,
    REG_VALUE_REAL,
    REG_VALUE_STRING,
    REG_VALUE_TIME,
    TIME_MAX_SECONDS,
    Colour,
    DataType,
    DisplayMode,
)

# Offsets within the 4100..4108 display block.
COLOUR_IDX, BRIGHT_IDX, BLINK_IDX = 0, 1, 2
MODE_IDX, TYPE_IDX, DP_IDX = 6, 7, 8


class FakeValue:
    def __init__(self, value):
        self.value = value


class FakeConfig:
    def __init__(self, **overrides):
        defaults = {
            "slave_id": 1,
            "default_colour": "green",
            "brightness": 75,
            "blank_timeout": 300.0,
            "safe_state_timeout": 0,
            "resync_interval": 30.0,
        }
        defaults.update(overrides)
        for key, value in defaults.items():
            setattr(self, key, FakeValue(value))


class FakeTag:
    def __init__(self, value=None):
        self._value = value

    async def set(self, value):
        self._value = value

    def get(self):
        return self._value


class FakeTags:
    _NAMES = (
        "displayed_value",
        "displayed_colour",
        "is_blank",
        "comms_ok",
        "last_error",
        "last_write_ts",
    )

    def __init__(self):
        for name in self._NAMES:
            setattr(self, name, FakeTag())


class FakeModbus:
    """Records writes; can be told to fail."""

    def __init__(self):
        self.writes: list[tuple[int, list[int]]] = []
        self.fail = False
        self.raise_on_write = False

    async def write_registers(self, *, modbus_id, start_address, values, register_type):
        if self.raise_on_write:
            raise OSError("bus fault")
        if self.fail:
            return False
        self.writes.append((start_address, list(values)))
        return True

    async def read_registers(self, **kwargs):
        return 99

    def pop(self):
        writes, self.writes = self.writes, []
        return writes

    def block(self):
        """The most recent 4100.. write, however it was coalesced."""
        for start, values in reversed(self.writes):
            if start == DISPLAY_BLOCK_START:
                return values
        return None


def make_app(**config_overrides):
    app = object.__new__(SMI2MApplication)
    app.config = FakeConfig(**config_overrides)
    app.tags = FakeTags()
    app.modbus_iface = FakeModbus()
    return app


@pytest.fixture
async def app():
    instance = make_app()
    await instance.setup()
    instance.modbus_iface.pop()  # discard the startup blank
    return instance


class TestSetup:
    async def test_starts_blank(self):
        instance = make_app()
        await instance.setup()
        # A fresh app must not inherit whatever the panel was left showing.
        assert instance.tags.is_blank.get() is True
        assert (REG_VALUE_IMAGE, [0, 0]) in instance.modbus_iface.writes

    async def test_safe_state_armed_when_configured(self):
        on = make_app(safe_state_timeout=60, resync_interval=30.0)
        await on.setup()
        # 4061..4066 collapse into a single coalesced write, byte order first.
        assert (4061, [0, 60, 0, 0, int(Colour.RED), 0]) in on.modbus_iface.writes

    async def test_safe_state_disarmed_when_configured_zero(self):
        off = make_app(safe_state_timeout=0)
        await off.setup()
        # Zero must be *written*, not skipped. Displays ship with an armed
        # timeout in their own flash; skipping the write leaves it armed
        # forever and the panel drops to its safe-state pattern between our
        # writes, which reads as the app sending garbage.
        assert (4061, [0, 0, 0, 0, int(Colour.RED), 0]) in off.modbus_iface.writes

    async def test_warns_when_failsafe_would_outpace_resync(self, caplog):
        app = make_app(safe_state_timeout=1, resync_interval=30.0)
        with caplog.at_level("WARNING"):
            await app.setup()
        assert "safe state" in caplog.text.lower()

    async def test_no_warning_when_failsafe_outlasts_resync(self, caplog):
        app = make_app(safe_state_timeout=60, resync_interval=30.0)
        with caplog.at_level("WARNING"):
            await app.setup()
        assert "safe state" not in caplog.text.lower()

    async def test_never_writes_save_to_flash(self):
        instance = make_app(safe_state_timeout=30)
        await instance.setup()
        await instance.show(42)
        await instance.show("HELLO")
        await instance._blank()
        # Register 5000 would burn the display's finite flash budget.
        assert all(start != 5000 for start, _ in instance.modbus_iface.writes)


class TestShowNumber:
    async def test_writes_display_block_and_real_value(self, app):
        await app.show(123.4)
        writes = dict(app.modbus_iface.pop())
        assert writes[REG_VALUE_REAL] == [17142, 52429]
        block = writes[DISPLAY_BLOCK_START]
        assert block[TYPE_IDX] == DataType.REAL
        assert block[MODE_IDX] == DisplayMode.STATIC
        assert block[DP_IDX] == 1

    async def test_oversized_value_scrolls_instead_of_erroring(self, app):
        await app.show(88888)
        assert app.modbus_iface.block()[MODE_IDX] == DisplayMode.NUMBER_TICKER

    async def test_numeric_string_is_shown_as_a_number(self, app):
        await app.show("12.5")
        assert dict(app.modbus_iface.pop())[REG_VALUE_REAL] == [16712, 0]

    async def test_as_text_forces_text_rendering(self, app):
        await app.show("0012", as_text=True)
        writes = dict(app.modbus_iface.pop())
        assert REG_VALUE_STRING in writes
        assert REG_VALUE_REAL not in writes

    async def test_explicit_decimals_respected(self, app):
        await app.show(5.0, decimals=1)
        assert app.modbus_iface.block()[DP_IDX] == 1

    async def test_rejects_unusable_value(self, app):
        with pytest.raises(ValueError):
            await app.show(None)


class TestShowText:
    async def test_short_text_is_static(self, app):
        await app.show("OK")
        writes = dict(app.modbus_iface.pop())
        assert writes[REG_STRING_LENGTH] == [4]
        assert len(writes[REG_VALUE_STRING]) == 16
        assert writes[DISPLAY_BLOCK_START][MODE_IDX] == DisplayMode.STATIC

    async def test_long_text_scrolls(self, app):
        await app.show("LEVEL HIGH")
        assert app.modbus_iface.block()[MODE_IDX] == DisplayMode.TEXT_TICKER


class TestColourAndStyle:
    async def test_colour_travels_in_the_same_write_as_the_value(self, app):
        await app.show(10, colour="red")
        # One transaction, so the panel never shows the new number in the old
        # colour: both live in the 4100 block.
        block = dict(app.modbus_iface.pop())[DISPLAY_BLOCK_START]
        assert block[COLOUR_IDX] == Colour.RED

    async def test_colour_persists_across_later_values(self, app):
        await app.show(10, colour="yellow")
        app.modbus_iface.pop()
        await app.show(11)
        assert app.tags.displayed_colour.get() == "yellow"

    async def test_blink_and_brightness(self, app):
        await app.show(10, blink=True, brightness=20)
        block = dict(app.modbus_iface.pop())[DISPLAY_BLOCK_START]
        assert block[BLINK_IDX] == 1
        assert block[BRIGHT_IDX] == 20

    async def test_brightness_clamped(self, app):
        await app.show(10, brightness=500)
        assert dict(app.modbus_iface.pop())[DISPLAY_BLOCK_START][BRIGHT_IDX] == 100

    async def test_invalid_colour_rejected(self, app):
        with pytest.raises(ValueError):
            await app.show(10, colour="blue")


class TestWriteDiffing:
    async def test_unchanged_value_writes_nothing(self, app):
        await app.show(42)
        app.modbus_iface.pop()
        await app.show(42)
        # A 1 Hz loop re-sending an unchanged panel would saturate the bus.
        assert app.modbus_iface.pop() == []

    async def test_changed_value_writes_only_the_value(self, app):
        await app.show(42)
        app.modbus_iface.pop()
        await app.show(43)
        starts = [start for start, _ in app.modbus_iface.pop()]
        assert starts == [REG_VALUE_REAL]

    async def test_failed_write_is_retried_next_time(self, app):
        await app.show(42)
        app.modbus_iface.pop()

        app.modbus_iface.fail = True
        with pytest.raises(RuntimeError):
            await app.show(43)
        assert app.tags.comms_ok.get() is False

        # The failed state must not be recorded as written, or the panel would
        # stay stale forever while the app believed it was up to date.
        app.modbus_iface.fail = False
        assert await app._flush() is True
        assert dict(app.modbus_iface.pop())[REG_VALUE_REAL] == [16940, 0]
        assert app.tags.comms_ok.get() is True

    async def test_exception_is_reported_not_raised_out_of_flush(self, app):
        app.modbus_iface.raise_on_write = True
        with pytest.raises(RuntimeError):
            await app.show(43)
        assert app.tags.comms_ok.get() is False
        assert "bus fault" in app.tags.last_error.get()

    async def test_resync_reasserts_everything(self, app):
        await app.show(42)
        app.modbus_iface.pop()
        app._last_resync = time.monotonic() - 999
        await app.main_loop()
        # Recovers a display that was power-cycled while we sat idle.
        starts = sorted(start for start, _ in app.modbus_iface.pop())
        assert starts == sorted([DISPLAY_BLOCK_START, REG_VALUE_REAL])


class TestBlankTimeout:
    async def test_default_timeout_is_armed(self, app):
        await app.show(42)
        assert app._expires_at is not None
        assert app.status()["blanks_in"] == pytest.approx(300, abs=2)

    async def test_zero_timeout_stays_up(self, app):
        await app.show(42, timeout=0)
        assert app._expires_at is None
        assert app.status()["blanks_in"] is None

    async def test_null_timeout_stays_up(self, app):
        await app.show(42, timeout=None)
        assert app._expires_at is None

    async def test_config_zero_means_permanent(self):
        instance = make_app(blank_timeout=0.0)
        await instance.setup()
        await instance.show(42)
        assert instance._expires_at is None

    async def test_loop_blanks_after_expiry(self, app):
        await app.show(42, timeout=5)
        app.modbus_iface.pop()

        app._expires_at = time.monotonic() - 0.01
        await app.main_loop()

        # Switching the data type to IMAGE is what darkens the panel. The
        # bitmask register itself is already 0 from the startup blank, so the
        # diff correctly leaves it alone rather than rewriting it.
        writes = dict(app.modbus_iface.pop())
        assert writes[DISPLAY_BLOCK_START][TYPE_IDX] == DataType.IMAGE
        assert REG_VALUE_IMAGE not in writes
        assert app.tags.is_blank.get() is True
        assert app._expires_at is None

    async def test_blanking_an_already_blank_panel_is_a_no_op(self, app):
        await app._blank()
        app.modbus_iface.pop()
        await app._blank()
        assert app.modbus_iface.pop() == []

    async def test_loop_does_not_blank_early(self, app):
        await app.show(42, timeout=60)
        app.modbus_iface.pop()
        await app.main_loop()
        assert app.modbus_iface.pop() == []
        assert app.tags.is_blank.get() is False

    async def test_blank_cancels_pending_timeout(self, app):
        await app.show(42, timeout=60)
        await app._blank()
        assert app._expires_at is None
        assert app.status()["blanks_in"] is None


class TestRPC:
    async def test_set_value_returns_status(self, app):
        result = await app.rpc_set_value(None, {"value": 42.5, "colour": "red"})
        assert result["displayed"] == "42.50"
        assert result["colour"] == "red"
        assert result["blank"] is False

    async def test_set_value_requires_a_value(self, app):
        from pydoover import rpc

        with pytest.raises(rpc.RPCError) as excinfo:
            await app.rpc_set_value(None, {"colour": "red"})
        assert excinfo.value.code == "INVALID_PARAMS"

    async def test_set_value_rejects_bad_colour_as_client_error(self, app):
        from pydoover import rpc

        with pytest.raises(rpc.RPCError) as excinfo:
            await app.rpc_set_value(None, {"value": 1, "colour": "puce"})
        assert excinfo.value.code == "INVALID_PARAMS"

    async def test_device_error_is_distinguished_from_bad_input(self, app):
        from pydoover import rpc

        app.modbus_iface.fail = True
        with pytest.raises(rpc.RPCError) as excinfo:
            await app.rpc_set_value(None, {"value": 1})
        assert excinfo.value.code == "DEVICE_ERROR"

    async def test_blank_rpc(self, app):
        await app.rpc_set_value(None, {"value": 42})
        result = await app.rpc_blank(None, {})
        assert result["blank"] is True

    async def test_set_colour_keeps_the_value(self, app):
        await app.rpc_set_value(None, {"value": 42})
        app.modbus_iface.pop()

        result = await app.rpc_set_colour(None, {"colour": "yellow"})
        assert result["colour"] == "yellow"
        assert result["displayed"] == "42"

        # Only the settings block moves; the value registers are untouched.
        starts = [start for start, _ in app.modbus_iface.pop()]
        assert starts == [DISPLAY_BLOCK_START]

    async def test_set_colour_accepts_a_bare_string(self, app):
        result = await app.rpc_set_colour(None, "red")
        assert result["colour"] == "red"

    async def test_get_status_reports_the_panel_state(self, app):
        await app.show(42)
        assert (await app.rpc_get_status(None, {}))["displayed"] == "42"


class TestCountdown:
    async def test_shows_the_first_value_immediately(self, app):
        result = await app.rpc_countdown(None, {"seconds": 300})
        assert result["counting_down"] is True
        assert result["displayed"] == "05:00"
        # Fired the instant the pump starts, not one loop later.
        assert dict(app.modbus_iface.pop())[REG_VALUE_TIME] is not None

    async def test_counts_down_on_each_tick(self, app):
        await app.rpc_countdown(None, {"seconds": 300})
        app._countdown_ends_at = time.monotonic() + 120.4
        await app.main_loop()
        # Ceiling: the sign reads 2:01 until the 121st second is fully gone.
        assert app.status()["displayed"] == "02:01"

    async def test_recomputed_from_deadline_not_decremented(self, app):
        await app.rpc_countdown(None, {"seconds": 300})
        app._countdown_ends_at = time.monotonic() + 10
        await app.main_loop()
        await app.main_loop()
        await app.main_loop()
        # Three ticks, but only real time advances the count — no drift.
        assert app.status()["displayed"] == "00:10"

    async def test_blanks_at_zero(self, app):
        await app.rpc_countdown(None, {"seconds": 5})
        app._countdown_ends_at = time.monotonic() - 0.01
        await app.main_loop()
        assert app.tags.is_blank.get() is True
        assert app.status()["counting_down"] is False

    async def test_can_hold_zero_instead_of_blanking(self, app):
        await app.rpc_countdown(None, {"seconds": 5, "blank_at_zero": False})
        app._countdown_ends_at = time.monotonic() - 0.01
        await app.main_loop()
        assert app.status()["displayed"] == "00:00"
        assert app.tags.is_blank.get() is False

    async def test_colour_thresholds(self, app):
        await app.rpc_countdown(
            None, {"seconds": 300, "warn_at": 60, "critical_at": 10}
        )
        assert app.status()["colour"] == "green"

        app._countdown_ends_at = time.monotonic() + 45
        await app.main_loop()
        assert app.status()["colour"] == "yellow"

        app._countdown_ends_at = time.monotonic() + 5
        await app.main_loop()
        assert app.status()["colour"] == "red"

    async def test_no_blank_timeout_while_counting(self, app):
        await app.rpc_countdown(None, {"seconds": 300})
        # The configured 300s blank must not fight the countdown for the panel.
        assert app._expires_at is None

    async def test_set_value_supersedes_a_countdown(self, app):
        await app.rpc_countdown(None, {"seconds": 300})
        await app.rpc_set_value(None, {"value": 42})
        assert app.status()["counting_down"] is False
        await app.main_loop()
        assert app.status()["displayed"] == "42"

    async def test_blank_cancels_a_countdown(self, app):
        await app.rpc_countdown(None, {"seconds": 300})
        await app.rpc_blank(None, {})
        assert app.status()["counting_down"] is False
        await app.main_loop()
        assert app.tags.is_blank.get() is True

    async def test_a_failed_write_does_not_end_the_countdown(self, app):
        await app.rpc_countdown(None, {"seconds": 300})
        app.modbus_iface.fail = True
        app._countdown_ends_at = time.monotonic() + 100
        await app.main_loop()
        # A dropped frame loses one tick; the next second re-asserts it.
        assert app.status()["counting_down"] is True
        app.modbus_iface.fail = False
        await app.main_loop()
        assert app.status()["displayed"] == "01:40"

    async def test_uses_the_panel_time_type_not_a_bare_number(self, app):
        """MM:SS comes from data type 7 and register 4252, not from us."""
        await app.rpc_countdown(None, {"seconds": 1200})
        written = dict(app.modbus_iface.pop())
        assert written[DISPLAY_BLOCK_START][TYPE_IDX] == int(DataType.TIME)
        # 1200 raw seconds; the display does the /60 itself.
        assert written[REG_VALUE_TIME] == [0, 1200]
        assert app.status()["displayed"] == "20:00"

    async def test_as_time_false_keeps_a_bare_second_count(self, app):
        await app.rpc_countdown(None, {"seconds": 300, "as_time": False})
        assert app.status()["displayed"] == "300"
        assert REG_VALUE_REAL in dict(app.modbus_iface.pop())

    async def test_rejects_a_count_too_long_for_mm_ss(self, app):
        from pydoover import rpc

        with pytest.raises(rpc.RPCError) as excinfo:
            await app.rpc_countdown(None, {"seconds": TIME_MAX_SECONDS + 1})
        assert excinfo.value.code == "INVALID_PARAMS"
        assert app.status()["counting_down"] is False

    async def test_a_long_count_is_allowed_as_raw_seconds(self, app):
        result = await app.rpc_countdown(
            None, {"seconds": TIME_MAX_SECONDS + 1, "as_time": False}
        )
        assert result["counting_down"] is True

    @pytest.mark.parametrize(
        "bad", [{}, {"seconds": 0}, {"seconds": -5}, {"seconds": "soon"}]
    )
    async def test_rejects_bad_duration(self, app, bad):
        from pydoover import rpc

        with pytest.raises(rpc.RPCError) as excinfo:
            await app.rpc_countdown(None, bad)
        assert excinfo.value.code == "INVALID_PARAMS"

    async def test_rejects_bad_colour_before_starting(self, app):
        from pydoover import rpc

        with pytest.raises(rpc.RPCError):
            await app.rpc_countdown(None, {"seconds": 60, "colour": "puce"})
        assert app.status()["counting_down"] is False
