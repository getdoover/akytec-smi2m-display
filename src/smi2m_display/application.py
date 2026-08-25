import logging
import math
import time

from pydoover import rpc
from pydoover.docker import Application

from .app_config import SMI2MConfig
from .app_tags import SMI2MTags
from .smi2m_driver import (
    DISPLAY_BLOCK_START,
    HOLDING_REGISTER,
    REG_FLASH_CYCLES_REMAINING,
    REG_SAFE_STATE_BITMASK,
    REG_SAFE_STATE_BLINKING,
    REG_SAFE_STATE_COLOUR,
    REG_SAFE_STATE_TIMEOUT,
    REG_STRING_LENGTH,
    REG_VALUE_IMAGE,
    REG_VALUE_REAL,
    REG_VALUE_STRING,
    Colour,
    DataType,
    DisplayMode,
    decimal_point_for,
    float_to_registers,
    parse_colour,
    sanitise_string,
    string_to_registers,
    uint32_to_registers,
    value_fits,
)

log = logging.getLogger(__name__)

#: The platform-default RPC channel ("dv-rpc"), which is what `rpc.call()`
#: targets when a caller names no channel. Handlers must state it explicitly:
#: registration only subscribes to a channel that is named, so a handler left
#: at channel=None is registered but never hears anything.
#:
#: Because this channel is shared with every other app on the device, callers
#: should pass ``app_key`` to address a specific display — see the README.
DISPLAY_CONTROL_CHANNEL = rpc.DEFAULT_CHANNEL

#: Sentinel so ``timeout=None`` ("never expire") is distinguishable from
#: "caller said nothing, use the configured default".
_UNSET = object()

#: Segment bitmask that lights nothing — see the module docstring in the driver.
BLANK_IMAGE = 0


class SMI2MApplication(Application):
    """Drives an akYtec SMI2-M 7-segment display over RS485/Modbus RTU.

    The display holds no state of its own worth trusting: everything this app
    writes lands in its RAM, so a power blip leaves the panel showing whatever
    it booted with. The app therefore treats itself as the source of truth and
    periodically re-asserts the full display state (``resync_interval``) rather
    than assuming a successful write stays applied.
    """

    config_cls = SMI2MConfig
    tags_cls = SMI2MTags

    config: SMI2MConfig
    tags: SMI2MTags

    # A one-second tick keeps the blank timeout accurate to the second without
    # putting meaningful traffic on the bus — writes only happen on change.
    loop_target_period = 1

    async def setup(self):
        # Desired vs last-confirmed register state. Diffing the two is what
        # keeps a 1 Hz loop from re-writing an unchanged panel forever.
        self._desired: dict[int, list[int]] = {}
        self._written: dict[int, list[int]] = {}

        self._expires_at: float | None = None
        # An active countdown: the monotonic deadline plus its presentation.
        # Held here rather than as a background task so it shares the main
        # loop's cadence and cannot outlive a blank or a new value.
        self._countdown_ends_at: float | None = None
        self._countdown_opts: dict = {}
        self._display_text = ""
        self._colour = parse_colour(self.config.default_colour.value)
        self._blink = False
        self._brightness = self.config.brightness.value
        # Start the clock now, so the first resync happens an interval from
        # startup rather than on the very first tick.
        self._last_resync = time.monotonic()

        await self._apply_safe_state_config()

        # Start from a known state rather than inheriting whatever the panel
        # was left showing by a previous run or another master.
        await self._blank()
        await self._publish_tags()

    # -----------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------

    async def main_loop(self):
        now = time.monotonic()

        if self._countdown_ends_at is not None:
            await self._tick_countdown(now)

        if self._expires_at is not None and now >= self._expires_at:
            log.info("Display value expired after its timeout; blanking")
            await self._blank()

        resync = self.config.resync_interval.value
        if resync and now - self._last_resync >= resync:
            self._last_resync = now
            # Forget what we think the panel has so the next flush rewrites
            # everything. This is the recovery path for a display that was
            # power-cycled while we were idle.
            self._written.clear()
            await self._flush()

        await self._publish_tags()

    async def _publish_tags(self):
        await self.tags.displayed_value.set(self._display_text)
        await self.tags.displayed_colour.set(self._colour.name.lower())
        await self.tags.is_blank.set(self._display_text == "")

        if self._expires_at is None:
            remaining = -1.0
        else:
            remaining = max(0.0, self._expires_at - time.monotonic())
        await self.tags.seconds_until_blank.set(round(remaining, 1))

    # -----------------------------------------------------------------
    # Display state
    # -----------------------------------------------------------------

    def _display_block(
        self, data_type: DataType, mode: DisplayMode, decimal_point: int
    ) -> list[int]:
        """Registers 4100..4108 as one contiguous run.

        Colour, brightness, blinking and data type all live in this block, so
        writing it whole means a value change and a colour change land in the
        same Modbus transaction — the panel never shows the new number in the
        old colour, however briefly.
        """
        return [
            int(self._colour),  # 4100 colour
            max(0, min(100, int(self._brightness))),  # 4101 brightness
            1 if self._blink else 0,  # 4102 blinking
            int(self.config.blink_period.value),  # 4103 blink period
            0,  # 4104 leading zeros
            int(self.config.scroll_tick.value),  # 4105 tick time
            int(mode),  # 4106 display mode
            int(data_type),  # 4107 data type
            int(decimal_point),  # 4108 decimal point
        ]

    def _show_number(self, value: float, decimals: int | None):
        # Values wider than the four digits scroll instead of showing the
        # panel's out-of-range error, which is almost never what an operator
        # wants to see in place of a reading.
        if value_fits(value):
            mode = DisplayMode.STATIC
            dp = decimal_point_for(value, decimals)
        else:
            mode = DisplayMode.NUMBER_TICKER
            dp = 0 if decimals is None else max(0, min(3, int(decimals)))

        swap_w = self.config.swap_words.value
        swap_b = self.config.swap_bytes.value

        self._desired = {
            DISPLAY_BLOCK_START: self._display_block(DataType.REAL, mode, dp),
            REG_VALUE_REAL: float_to_registers(value, swap_w, swap_b),
        }
        self._display_text = self._render_number(value, dp)

    @staticmethod
    def _render_number(value: float, decimal_point: int) -> str:
        """Best-effort echo of what the panel now reads, for the status tag."""
        if decimal_point == 0 and float(value).is_integer():
            return str(int(value))
        return f"{value:.{decimal_point}f}" if decimal_point else str(value)

    def _show_text(self, text: str):
        cleaned = sanitise_string(text)
        scroll = self.config.scroll_long_text.value and len(cleaned.strip()) > 4
        mode = DisplayMode.TEXT_TICKER if scroll else DisplayMode.STATIC

        registers, length = string_to_registers(cleaned, self.config.swap_bytes.value)
        self._desired = {
            DISPLAY_BLOCK_START: self._display_block(DataType.STRING, mode, 0),
            REG_VALUE_STRING: registers,
            REG_STRING_LENGTH: [length],
        }
        self._display_text = cleaned

    async def _blank(self):
        """Turn every segment off.

        Done as an IMAGE write of an empty bitmask rather than by writing 0 or
        spaces, so the panel is genuinely dark instead of reading "0".
        """
        self._desired = {
            DISPLAY_BLOCK_START: self._display_block(
                DataType.IMAGE, DisplayMode.STATIC, 0
            ),
            REG_VALUE_IMAGE: uint32_to_registers(
                BLANK_IMAGE,
                self.config.swap_words.value,
                self.config.swap_bytes.value,
            ),
        }
        self._display_text = ""
        self._expires_at = None
        await self._flush()
        await self._publish_tags()

    def _arm_timeout(self, timeout):
        """Set (or clear) the moment the current value blanks itself."""
        if timeout is _UNSET:
            timeout = self.config.blank_timeout.value
        if timeout is None:
            self._expires_at = None
            return
        timeout = float(timeout)
        # 0 means "leave it up" — the configured default and an explicit 0
        # agree on that, so a permanent sign is just blank_timeout=0.
        self._expires_at = time.monotonic() + timeout if timeout > 0 else None

    # -----------------------------------------------------------------
    # Countdown
    # -----------------------------------------------------------------

    async def _tick_countdown(self, now: float):
        """Advance an active countdown by one loop.

        The remaining time is recomputed from the deadline every tick rather
        than decremented, so a slow loop or a missed Modbus write loses a frame
        instead of desynchronising the clock from real time — a sign counting
        down to something physical must not drift.
        """
        remaining = self._countdown_ends_at - now
        opts = self._countdown_opts

        if remaining <= 0:
            self._countdown_ends_at = None
            self._countdown_opts = {}
            if opts.get("blank_at_zero", True):
                await self._blank()
            else:
                await self._apply(0, opts)
            return

        # Ceiling, so the sign reads "1" for the whole final second and hits
        # zero exactly when the time is actually up.
        await self._apply(math.ceil(remaining), opts)

    async def _apply(self, seconds: int, opts: dict):
        colour = self._countdown_colour(seconds, opts)
        try:
            await self.show(seconds, colour=colour, timeout=None, decimals=0)
        except (ValueError, RuntimeError) as exc:
            # A dropped write is not worth ending the countdown over; the next
            # tick a second later re-asserts the value anyway.
            log.debug("Countdown tick failed: %s", exc)

    @staticmethod
    def _countdown_colour(seconds: int, opts: dict):
        critical, warn = opts.get("critical_at"), opts.get("warn_at")
        if critical is not None and seconds <= critical:
            return Colour.RED
        if warn is not None and seconds <= warn:
            return Colour.YELLOW
        return opts.get("colour")

    def _cancel_countdown(self):
        """Stop any countdown. Called whenever something else claims the panel."""
        self._countdown_ends_at = None
        self._countdown_opts = {}

    # -----------------------------------------------------------------
    # Modbus
    # -----------------------------------------------------------------

    async def _flush(self) -> bool:
        """Write whatever differs between desired and last-confirmed state."""
        pending = {
            addr: values
            for addr, values in sorted(self._desired.items())
            if self._written.get(addr) != values
        }
        if not pending:
            return True

        for start, values in self._coalesce(pending):
            if not await self._write(start, values):
                # Leave _written as-is: the next loop retries the same diff.
                return False

        self._written.update(pending)
        await self.tags.comms_ok.set(True)
        await self.tags.last_error.set("")
        await self.tags.last_write_ts.set(time.time())
        return True

    @staticmethod
    def _coalesce(blocks: dict[int, list[int]]) -> list[tuple[int, list[int]]]:
        """Merge adjacent register runs into single writes.

        The display block (4100..4108) and the REAL value (4206..4207) are
        separate runs, but a caller that later adds a neighbouring register
        gets the merge for free rather than an extra round trip on a 9600 baud
        bus where every transaction costs real milliseconds.
        """
        runs: list[tuple[int, list[int]]] = []
        for start in sorted(blocks):
            values = blocks[start]
            if runs:
                prev_start, prev_values = runs[-1]
                if prev_start + len(prev_values) == start:
                    runs[-1] = (prev_start, prev_values + values)
                    continue
            runs.append((start, list(values)))
        return runs

    async def _write(self, start: int, values: list[int]) -> bool:
        try:
            ok = await self.modbus_iface.write_registers(
                modbus_id=self.config.slave_id.value,
                start_address=start,
                values=values,
                register_type=HOLDING_REGISTER,
            )
        except Exception as exc:
            await self._record_failure(f"write {start}: {exc}")
            log.exception("Modbus write failed at register %d", start)
            return False

        # write_registers reports failure by return value as well as by
        # raising, and a silently-dropped write would strand the panel showing
        # a stale value while the app believed it had updated.
        if ok is False:
            await self._record_failure(f"write {start} rejected by device")
            return False
        return True

    async def _record_failure(self, message: str):
        await self.tags.comms_ok.set(False)
        await self.tags.last_error.set(message[:200])

    async def _apply_safe_state_config(self):
        """Configure the display's own comms-loss failsafe, if asked for.

        This is the only place the app touches configuration registers rather
        than value registers, and it still does not write Save-to-Flash: the
        setting is re-applied on every app start, which costs one write per
        boot instead of spending the panel's finite flash budget.

        The setting is written **unconditionally**, including the disabling
        zero. Skipping the write when the app wants it off would only ever arm
        the failsafe and never disarm it: a display carrying an armed timeout
        in its own flash (they ship with one) would keep it forever, and the
        symptom is brutal to read — the panel shows the value for an instant
        after each write and sits on the safe-state pattern in between, so it
        looks like the app is writing garbage rather than like a failsafe
        firing on schedule.
        """
        timeout = int(self.config.safe_state_timeout.value or 0)

        # An armed failsafe shorter than the resync gap means the panel spends
        # most of its life in the safe state, blanking between our writes.
        resync = self.config.resync_interval.value
        if timeout > 0 and resync and timeout <= resync:
            log.warning(
                "Safe state timeout (%ss) is not longer than the resync "
                "interval (%ss): the display will fall back to its safe state "
                "between writes. Raise the timeout or lower the resync.",
                timeout,
                resync,
            )

        # These four registers happen to be contiguous (4062..4066), but naming
        # them individually and letting _coalesce merge the run keeps the
        # offsets honest against the datasheet instead of hiding them in
        # index arithmetic.
        blocks = {
            REG_SAFE_STATE_TIMEOUT: [timeout],
            REG_SAFE_STATE_BITMASK: uint32_to_registers(
                BLANK_IMAGE,
                self.config.swap_words.value,
                self.config.swap_bytes.value,
            ),
            REG_SAFE_STATE_COLOUR: [int(Colour.RED)],
            REG_SAFE_STATE_BLINKING: [0],
        }
        for start, values in self._coalesce(blocks):
            if not await self._write(start, values):
                log.warning("Could not configure the display safe-state failsafe")
                return
        if timeout > 0:
            log.info("Display safe-state failsafe armed at %ds", timeout)
        else:
            log.info("Display safe-state failsafe disabled")

    async def _read_flash_budget(self):
        try:
            result = await self.modbus_iface.read_registers(
                modbus_id=self.config.slave_id.value,
                start_address=REG_FLASH_CYCLES_REMAINING,
                num_registers=1,
                register_type=HOLDING_REGISTER,
            )
        except Exception as exc:  # noqa: BLE001 - see below
            # Deliberately broad: this is a diagnostics-only read, and the
            # modbus interface surfaces transport, gRPC and device faults as
            # unrelated exception types. Narrowing it would let an unlisted one
            # fail the RPC that merely asked for status.
            log.debug("Could not read flash budget: %s", exc)
            return
        if isinstance(result, int):
            await self.tags.flash_cycles_remaining.set(result)

    # -----------------------------------------------------------------
    # Public entry point behind the RPC surface
    # -----------------------------------------------------------------

    async def show(
        self,
        value,
        colour=None,
        timeout=_UNSET,
        decimals: int | None = None,
        blink: bool | None = None,
        brightness: int | None = None,
        as_text: bool | None = None,
    ) -> dict:
        """Put *value* on the panel. Raises ValueError on unusable input."""
        if colour is not None:
            self._colour = parse_colour(colour)
        if blink is not None:
            self._blink = bool(blink)
        if brightness is not None:
            self._brightness = max(0, min(100, int(brightness)))

        number = None if as_text else self._as_number(value)
        if number is None:
            if value is None:
                raise ValueError("no value given")
            self._show_text(str(value))
        else:
            # A caller that hands over 42 means a count, not a measurement, so
            # it shows as "42" rather than the panel filling its spare digits
            # with "42.00". A float or a decimal string keeps fitted precision.
            if decimals is None and self._looks_integral(value):
                decimals = 0
            self._show_number(number, decimals)

        self._arm_timeout(timeout)

        if not await self._flush():
            raise RuntimeError(self.tags.last_error.get() or "modbus write failed")

        await self._publish_tags()
        return self.status()

    @staticmethod
    def _looks_integral(value) -> bool:
        """Whether the caller expressed the value as a whole number."""
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        if isinstance(value, str):
            return "." not in value and "e" not in value.lower()
        return False

    @staticmethod
    def _as_number(value):
        """Interpret *value* as a number, or return None if it is really text.

        Strings that parse cleanly are treated as numbers so a reading handed
        over as ``"12.5"`` — which is what a JSON payload or a text box tends
        to produce — is displayed right-aligned with a decimal point rather
        than as a scrolling word.
        """
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None

    def status(self) -> dict:
        remaining = (
            None
            if self._expires_at is None
            else max(0.0, round(self._expires_at - time.monotonic(), 1))
        )
        return {
            "displayed": self._display_text,
            "colour": self._colour.name.lower(),
            "blank": self._display_text == "",
            "blink": self._blink,
            "brightness": self._brightness,
            "blanks_in": remaining,
            "counting_down": self._countdown_ends_at is not None,
            "countdown_remaining": (
                None
                if self._countdown_ends_at is None
                else max(0, math.ceil(self._countdown_ends_at - time.monotonic()))
            ),
            "comms_ok": bool(self.tags.comms_ok.get()),
        }

    # -----------------------------------------------------------------
    # RPC surface
    # -----------------------------------------------------------------

    @rpc.handler("set_value", channel=DISPLAY_CONTROL_CHANNEL)
    async def rpc_set_value(self, ctx, payload: dict) -> dict:
        """Show a value.

        Payload::

            {
              "value": 42.5,          # number or text (required)
              "colour": "red",        # green | red | yellow (optional)
              "timeout": 120,         # seconds until blank; 0/null = stay up
              "decimals": 1,          # 0..3, else fitted automatically
              "blink": false,
              "brightness": 75,       # 0..100 %
              "as_text": false        # force text rendering of a numeric string
            }
        """
        if not isinstance(payload, dict):
            raise rpc.RPCError("INVALID_PARAMS", "payload must be an object")
        if "value" not in payload:
            raise rpc.RPCError("INVALID_PARAMS", "'value' is required")

        self._cancel_countdown()

        try:
            return await self.show(
                payload["value"],
                colour=payload.get("colour"),
                timeout=payload.get("timeout", _UNSET),
                decimals=payload.get("decimals"),
                blink=payload.get("blink"),
                brightness=payload.get("brightness"),
                as_text=payload.get("as_text"),
            )
        except ValueError as exc:
            raise rpc.RPCError("INVALID_PARAMS", str(exc)) from exc
        except RuntimeError as exc:
            raise rpc.RPCError("DEVICE_ERROR", str(exc)) from exc

    @rpc.handler("blank", channel=DISPLAY_CONTROL_CHANNEL)
    async def rpc_blank(self, ctx, payload) -> dict:
        """Blank the panel immediately, cancelling any timeout or countdown."""
        self._cancel_countdown()
        await self._blank()
        return self.status()

    @rpc.handler("countdown", channel=DISPLAY_CONTROL_CHANNEL)
    async def rpc_countdown(self, ctx, payload: dict) -> dict:
        """Count down to zero on the panel, one second at a time.

        The caller fires this once and the display owns the clock from there,
        rather than being fed a value every second: one message instead of
        hundreds, and the count keeps time even if the caller is busy.

        Payload::

            {
              "seconds": 300,        # required, > 0
              "colour": "green",     # colour above any threshold
              "warn_at": 60,         # yellow at or below this many seconds
              "critical_at": 10,     # red at or below this many seconds
              "blank_at_zero": true  # false leaves "0" showing
            }

        `blank` cancels it; so does any `set_value`.
        """
        if not isinstance(payload, dict):
            raise rpc.RPCError("INVALID_PARAMS", "payload must be an object")
        try:
            seconds = float(payload["seconds"])
        except (KeyError, TypeError, ValueError):
            raise rpc.RPCError(
                "INVALID_PARAMS", "'seconds' is required and must be a number"
            )
        if not math.isfinite(seconds) or seconds <= 0:
            raise rpc.RPCError("INVALID_PARAMS", "'seconds' must be greater than zero")

        # Validate the colours up front, so a typo fails the call instead of
        # surfacing a second later as a dead countdown mid-tick.
        opts = {
            "colour": payload.get("colour"),
            "warn_at": payload.get("warn_at"),
            "critical_at": payload.get("critical_at"),
            "blank_at_zero": bool(payload.get("blank_at_zero", True)),
        }
        for key in ("colour",):
            if opts[key] is not None:
                try:
                    parse_colour(opts[key])
                except ValueError as exc:
                    raise rpc.RPCError("INVALID_PARAMS", str(exc)) from exc

        self._countdown_opts = opts
        self._countdown_ends_at = time.monotonic() + seconds
        # Show the first value now rather than after one loop, so the sign
        # reacts the instant the pump starts.
        await self._apply(math.ceil(seconds), opts)
        log.info("Counting down from %ds", math.ceil(seconds))
        return self.status()

    @rpc.handler("set_colour", channel=DISPLAY_CONTROL_CHANNEL)
    async def rpc_set_colour(self, ctx, payload) -> dict:
        """Recolour what is already on the panel, without changing the value."""
        colour = payload.get("colour") if isinstance(payload, dict) else payload
        try:
            self._colour = parse_colour(colour)
        except ValueError as exc:
            raise rpc.RPCError("INVALID_PARAMS", str(exc)) from exc

        # Re-emit the display block with the new colour; the value registers
        # are unchanged so the diff sends a single 9-register write.
        if DISPLAY_BLOCK_START in self._desired:
            block = list(self._desired[DISPLAY_BLOCK_START])
            block[0] = int(self._colour)
            self._desired[DISPLAY_BLOCK_START] = block
        if not await self._flush():
            raise rpc.RPCError(
                "DEVICE_ERROR", self.tags.last_error.get() or "modbus write failed"
            )
        return self.status()

    @rpc.handler("get_status", channel=DISPLAY_CONTROL_CHANNEL)
    async def rpc_get_status(self, ctx, payload) -> dict:
        """Report what the panel is showing and whether the bus is healthy."""
        await self._read_flash_budget()
        return self.status()
