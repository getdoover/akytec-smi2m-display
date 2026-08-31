"""Register map and encoders for the akYtec SMI2-M RS485 multi-colour display.

Reference: UG_SMI2-M_2022.02_0001_EN, Appendix B (Table B.1 Modbus registers).

The display is driven in its **SLAVE** operation mode: it sits on the RS485 bus
as a Modbus RTU slave and we, the Doovit, are the master writing holding
registers into it. Every "parameter" in the manual — colour, brightness, data
type, and the displayed value itself — is just a holding register, so showing a
number is a plain FC16 write.

Two things in here are load-bearing and easy to get wrong:

**Never write register 5000 (Save-to-Flash) on the value path.** The display's
configuration lives in RAM and is mirrored to flash only when that trigger is
written. Flash has a finite write budget — the device exposes what is left of it
at register 61624 — so a firmware that "saved" on every update would wear the
part out. Everything this driver writes for a normal value update is RAM-only
and simply reverts on power cycle, which is what we want: the app re-asserts the
full display state on startup anyway.

**Blanking is an IMAGE write, not a value write.** There is no "off" register.
IMAGE data type (6) interprets register 4250 as a bitmask of lit segments
(Table 4.8), so a mask of 0 lights nothing and the panel goes dark. That is a
genuine blank rather than showing "0" or a stale reading.

**Every 32-bit value goes out low word first.** The panel stores a REAL32 or a
UINT32 with the least significant register at the lower address, which is the
opposite of the byte order the Modbus wire format suggests. This is not
configurable away: register 4061 accepts 0..3 but none of them changed the
ordering on a live unit. Two independent confirmations, both on hardware:

* The panel's own factory default *factor* (a REAL32 of 1.0) reads back from
  registers 4111/4112 as ``[0x0000, 0x3F80]`` — low word at the lower address.
* Writing ``[0x0000, 0x005A]`` to the TIME registers is rejected with Modbus
  exception 3, because the panel reads it as 90 << 16 seconds, far past its
  5999 s limit; ``[0x005A, 0x0000]`` is accepted and shows ``01:30``.

Beware the second one in particular: the TIME type is the only value field the
panel range-checks, so it is the only one that *tells you* the order is wrong.
A REAL in the wrong order is accepted silently and simply shows the wrong
number. A read-back proves nothing either — the registers echo whatever was
written, whatever the panel makes of it.
"""

from __future__ import annotations

import math
import struct
from enum import IntEnum

# --------------------------------------------------------------------------
# Modbus plumbing
# --------------------------------------------------------------------------

#: pydoover's register-type code for holding registers (FC03 read / FC16 write).
HOLDING_REGISTER = 4

#: Factory-default slave address. The manual notes the akYtecToolPro search
#: always finds the device at address 1 regardless of configuration.
DEFAULT_SLAVE_ID = 1


# --------------------------------------------------------------------------
# Register addresses (decimal, per Table B.1)
# --------------------------------------------------------------------------

# -- Device settings --
REG_OPERATION_MODE = 4000  # ENUM3: 0 SLAVE, 1 MASTER, 2 SPY
REG_SAVE_TO_FLASH = 5000  # ENUM2: writing 1 commits RAM config to flash

# -- Modbus common --
REG_BYTE_ORDER = 4061  # ENUM4: 0 unchanged, 1 swap bytes, 2 swap regs, 3 both
#: Written as 0 (UNCHANGED) at startup: the encoders below match the panel's
#: native ordering, so there is nothing to swap. Setting it to 1, 2 or 3 was
#: tried on a live unit and changed nothing about how a written value was
#: interpreted, so this register is pinned rather than trusted. Zero is
#: invariant under every swap the display can apply, so the write lands
#: correctly whatever order the panel was carrying beforehand.
REG_SAFE_STATE_TIMEOUT = 4062  # UINT16, 0..60 s (0 disables)
REG_SAFE_STATE_BITMASK = 4063  # UINT32 segment mask shown on comms loss
REG_SAFE_STATE_COLOUR = 4065  # ENUM3
REG_SAFE_STATE_BLINKING = 4066  # ENUM2

# -- Display (4100..4108 is one contiguous run; see DISPLAY_BLOCK below) --
REG_COLOUR = 4100  # ENUM3: 0 green, 1 red, 2 yellow
REG_BRIGHTNESS = 4101  # UINT8, 0..100 %
REG_BLINKING = 4102  # ENUM2: 0 off, 1 on
REG_BLINK_PERIOD = 4103  # UINT16, 250..3000 ms
REG_LEADING_ZEROS = 4104  # ENUM4: 0..3
REG_TICK_TIME = 4105  # UINT16, 100..1500 ms
REG_DISPLAY_MODE = 4106  # ENUM3: 0 static, 1 text ticker, 2 number ticker
REG_DATA_TYPE = 4107  # ENUM8, see DataType
REG_DECIMAL_POINT = 4108  # ENUM8: 0 "----", 1 "---.-", 2 "--.--", 3 "-.---"
REG_OFFSET = 4109  # REAL32 (2 registers)
REG_FACTOR = 4111  # REAL32 (2 registers)

# -- Displayed value --
REG_VALUE_INT = 4200  # INT16
REG_VALUE_UINT = 4201  # UINT16
REG_VALUE_DINT = 4202  # INT32  (2 registers)
REG_VALUE_UDINT = 4204  # UINT32 (2 registers)
REG_VALUE_REAL = 4206  # REAL32 (2 registers)
REG_VALUE_STRING = 4208  # 16 registers / 32 chars in SLAVE mode
REG_STRING_LENGTH = 4249  # UINT8, 4..32
REG_VALUE_IMAGE = 4250  # UINT32 segment bitmask (2 registers)
REG_VALUE_TIME = 4252  # UINT32 seconds, displayed as MM:SS (2 registers)

# -- Device status (read-only) --
REG_STATUS = 61620  # 2 registers
REG_FLASH_CYCLES_REMAINING = 61624  # % of flash write budget left

#: In SLAVE mode the manual is explicit: the string field is always 16
#: registers (32 characters) and "read / write of a string fragment is not
#: possible". Short strings must therefore be space-padded to the full width.
STRING_REGISTERS = 16
STRING_MAX_CHARS = STRING_REGISTERS * 2

#: The display's own numeric range. Values outside it show an out-of-range
#: error on the panel (Table 4.11) unless number-ticker mode is used.
DISPLAY_MIN = -999.0
DISPLAY_MAX = 9999.0

#: Number of 7-segment digits on the panel.
DIGITS = 4


class DataType(IntEnum):
    """Register 4107 — how the display interprets its value registers."""

    INT = 0
    UINT = 1
    DINT = 2
    UDINT = 3
    REAL = 4
    STRING = 5
    IMAGE = 6
    TIME = 7


class Colour(IntEnum):
    """Register 4100. The panel is a tri-colour (red/green) LED matrix."""

    GREEN = 0
    RED = 1
    YELLOW = 2


class DisplayMode(IntEnum):
    """Register 4106.

    ``TEXT_TICKER`` scrolls character by character and is the only way to show
    a string longer than the four physical digits. ``NUMBER_TICKER`` scrolls
    numbers without the -999..9999 range check, so it is how you display a
    value with more than four digits.
    """

    STATIC = 0
    TEXT_TICKER = 1
    NUMBER_TICKER = 2


#: Registers 4100..4108 inclusive — colour through decimal point. Writing the
#: whole run in one FC16 is how we push a full display state atomically.
DISPLAY_BLOCK_START = REG_COLOUR
DISPLAY_BLOCK_END = REG_DECIMAL_POINT

COLOUR_BY_NAME = {
    "green": Colour.GREEN,
    "red": Colour.RED,
    "yellow": Colour.YELLOW,
    # The panel has no amber emitter; amber/orange are the same mixed colour.
    "amber": Colour.YELLOW,
    "orange": Colour.YELLOW,
}


def parse_colour(value) -> Colour:
    """Coerce a colour name or raw enum value to a :class:`Colour`.

    Raises
    ------
    ValueError
        If the colour is not one the hardware can produce.
    """
    if isinstance(value, Colour):
        return value
    # bool is an int subclass; let it fall through to the final raise rather
    # than being read as colour code 0/1.
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return Colour(value)
        except ValueError:
            raise ValueError(f"invalid colour code: {value!r}") from None
    if isinstance(value, str):
        try:
            return COLOUR_BY_NAME[value.strip().lower()]
        except KeyError:
            raise ValueError(
                f"invalid colour {value!r}; expected one of "
                f"{', '.join(sorted(COLOUR_BY_NAME))}"
            ) from None
    raise ValueError(f"invalid colour: {value!r}")


# --------------------------------------------------------------------------
# Word encoding
# --------------------------------------------------------------------------
#
# pydoover's modbus interface takes and returns registers as plain ints, so
# anything wider than 16 bits has to be split here. The display applies its own
# Byte order parameter (register 4061) on top of whatever we send, and the app
# pins that to UNCHANGED at startup — so the encoding here is always the usual
# Modbus convention: 0xAABBCCDD goes out as [0xAABB, 0xCCDD], most significant
# word first.


def float_to_registers(value: float) -> list[int]:
    """Encode a Python float as two registers of IEEE-754 single precision.

    Low word first — see the note on 32-bit ordering in the module docstring.
    """
    if not math.isfinite(value):
        raise ValueError(f"cannot display non-finite value: {value!r}")
    high, low = struct.unpack(">HH", struct.pack(">f", float(value)))
    return [low, high]


def uint32_to_registers(value: int) -> list[int]:
    """Encode an unsigned 32-bit integer as two registers, low word first."""
    value = int(value)
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"value out of UINT32 range: {value}")
    return [value & 0xFFFF, (value >> 16) & 0xFFFF]


# --------------------------------------------------------------------------
# String encoding
# --------------------------------------------------------------------------

#: Table B.4 restricts the panel to the Latin alphabet, digits, space and the
#: decimal point. Anything else has no segment pattern, so we substitute rather
#: than let the display render something arbitrary.
_STRING_SUBSTITUTIONS = {
    "_": "-",
    "–": "-",  # en dash
    "—": "-",  # em dash
    "\t": " ",
    "\n": " ",
}

_ALLOWED_EXTRA = set(" .-")


def sanitise_string(text: str) -> str:
    """Reduce arbitrary text to characters the panel can actually render.

    Unrepresentable characters become ``-`` rather than being dropped, so the
    operator can see that something was lost instead of silently reading a
    word with a hole in it.
    """
    out = []
    for char in str(text):
        char = _STRING_SUBSTITUTIONS.get(char, char)
        if char.isascii() and (char.isalnum() or char in _ALLOWED_EXTRA):
            out.append(char)
        else:
            out.append("-")
    return "".join(out)


def string_to_registers(text: str) -> tuple[list[int], int]:
    """Encode text into the display's 16-register string field.

    Returns the register list (always :data:`STRING_REGISTERS` long, because
    SLAVE mode forbids partial string writes) and the visible character count
    to write into :data:`REG_STRING_LENGTH`.

    The length register is clamped to the documented 4..32 range: writing a
    smaller length is rejected by the device, and a three-character message
    padded to four simply shows a trailing space.
    """
    text = sanitise_string(text)[:STRING_MAX_CHARS]
    visible = max(4, min(STRING_MAX_CHARS, len(text)))
    padded = text.ljust(STRING_MAX_CHARS, " ")

    registers = [
        (ord(padded[i]) << 8) | ord(padded[i + 1])
        for i in range(0, STRING_MAX_CHARS, 2)
    ]
    return registers, visible


# --------------------------------------------------------------------------
# Value planning
# --------------------------------------------------------------------------


def integer_digits(value: float) -> int:
    """How many digit cells the integer part of *value* occupies (min 1)."""
    whole = int(abs(float(value)))
    return len(str(whole))


def decimal_point_for(value: float, requested: int | None = None) -> int:
    """Pick the decimal-point register value (0..3) for a numeric value.

    Register 4108 positions a *fixed* point, so with only four digit cells the
    available precision is whatever the integer part and the minus sign leave
    behind::

        decimals = 4 - (1 if negative) - digits(integer part)

    That formula is not a guess — it reproduces every worked example in the
    manual (Examples 5 and 6): ``5`` → ``5.000``, ``-5`` → ``-5.00``, ``10`` →
    ``10.00``, ``28.38`` → ``28.38``, ``-28.39`` → ``-28.3``.

    Fitting the maximum precision is the right default because a flow reading
    shown as ``12.34`` carries more than ``12``; callers who want a fixed
    presentation across a changing magnitude pass *requested* instead.
    """
    if requested is not None:
        return max(0, min(3, int(requested)))

    sign_cells = 1 if float(value) < 0 else 0
    return max(0, min(3, DIGITS - sign_cells - integer_digits(value)))


def value_fits(value: float) -> bool:
    """Whether a numeric value is inside the panel's static display range."""
    return DISPLAY_MIN <= value <= DISPLAY_MAX


# --------------------------------------------------------------------------
# TIME encoding
# --------------------------------------------------------------------------
#
# Data type 7 (TIME) is a *formatting* mode, not a clock: the display takes the
# UINT32 in register 4252 and renders it as MM:SS by integer division — the
# manual's footnote to Table 4.7 spells it out, "XX = N / 60 (integer
# quotient), YY = N / 60 (remainder). If N = 1000, 16:40 is displayed". It does
# not decrement anything, so a countdown still writes a new value every second;
# what TIME buys is that 1200 reads as 20:00 instead of as a meaningless
# four-digit number.
#
# Leading zeros, decimal point, offset and factor are documented (Table 4.6) as
# applying to integer and floating-point variables only, so none of them touch
# a TIME value.

#: Highest value the TIME type accepts (Table 4.7), i.e. 99:59. Above this the
#: panel shows the out-of-range error ErrH (Table 4.11) rather than wrapping.
TIME_MAX_SECONDS = 5999


def time_fits(seconds: int) -> bool:
    """Whether a second count can be shown as MM:SS on the panel."""
    return 0 <= int(seconds) <= TIME_MAX_SECONDS


def format_time(seconds: int) -> str:
    """Render *seconds* the way the panel will, for the status tag.

    Mirrors the display's own integer division so the tag and the glass agree.
    """
    seconds = int(seconds)
    if not time_fits(seconds):
        raise ValueError(
            f"{seconds}s is outside the display's MM:SS range (0..{TIME_MAX_SECONDS})"
        )
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
