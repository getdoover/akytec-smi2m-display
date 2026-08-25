# SMI2-M Display

Drives an [akYtec SMI2-M](https://d27h1xy6kx2vdd.cloudfront.net/downloads/SMI2-M/UG_SMI2-M_2022.02_0001_EN.pdf)
RS485 multi-colour 7-segment display from a Doover device, over Modbus RTU.

Other apps put values on the sign by calling this app over RPC. A value can
carry a colour and a timeout, so a reading pushed to a big display in a shed
blanks itself instead of sitting there stale for a week.

## How it talks to the display

The SMI2-M supports three Modbus roles. This app uses **SLAVE** mode — the
display's factory default — where the display sits on the bus as a Modbus
slave and the Doovit is the master writing holding registers into it.

That is the mode worth using here. In MASTER mode the display polls a single
slave register on a fixed timer, and in SPY mode it eavesdrops on someone
else's poll; both hard-wire the sign to one number chosen at commissioning
time. As a slave it shows whatever we write, whenever we write it, which is
what makes an RPC-driven sign possible at all.

Everything is a holding register (FC03 to read, FC16 to write) — colour,
brightness, data type and the displayed value alike. Showing a number is one
write of the 4100..4108 settings block plus one write of the value.

## RPC interface

This is a machine-only app — there is no site-page UI. Everything is driven by
other apps over RPC, on **`dv-rpc`**: the platform-default channel that
`rpc.call()` uses when a caller names no channel.

Because that channel is shared with every other app on the device, and this app
is `allow_many` (a device can run two signs), **pass `app_key` to address a
specific display**. Without it, every installed copy answers the same call.

### `set_value`

```jsonc
{
  "value": 42.5,      // number or text — required
  "colour": "red",    // green | red | yellow  (optional)
  "timeout": 120,     // seconds until it blanks; 0 or null = stay up
  "decimals": 1,      // 0..3; omitted = fitted automatically
  "blink": false,
  "brightness": 75,   // 0..100 %
  "as_text": false    // force "0012" to render as text, not the number 12
}
```

Returns the resulting display state:

```json
{
  "displayed": "42.5", "colour": "red", "blank": false,
  "blink": false, "brightness": 75, "blanks_in": 120.0, "comms_ok": true
}
```

From another app:

```python
await self.rpc.call(
    "set_value",
    {
        "value": tank_level,
        "colour": "red" if tank_level > 90 else "green",
        "timeout": 300,
    },
    app_key=self.config.display_app_key.value,  # which sign to drive
)
```

No `channel=` is needed: `rpc.call()` already defaults to `dv-rpc`. On the
handler side the app states the channel explicitly, because registration only
subscribes to a channel that is named.

### `blank`, `set_colour`, `get_status`

- **`blank`** — turn the panel off now and cancel any pending timeout.
- **`set_colour`** — `{"colour": "yellow"}`; recolours what is already shown
  without disturbing the value.
- **`get_status`** — the same status object, plus a refresh of the display's
  remaining flash-write budget.

## Values, and what actually fits

The panel is four 7-segment digits, so a decimal point costs a digit and a
minus sign costs one too. Unless you pass `decimals`, the app fits the most
precision that leaves room for the integer part:

| Value | Shows |
|-------|-------|
| `5` | `5.000` |
| `-5` | `-5.00` |
| `123.4` | `123.4` |
| `9999` | `9999` |
| `88888` | scrolls |

Numbers outside -999..9999 would normally trip the display's out-of-range
error, which is a poor thing to show an operator in place of a reading, so the
app switches those to number-ticker mode and scrolls them instead.

Text is limited to letters, digits, space, `.` and `-` — the character set the
panel has segment patterns for (Table B.4). Anything else becomes `-`, so a
lost character is visible rather than silently closing up. Strings longer than
four characters scroll, unless **Scroll Long Text** is off.

## Blanking

There is no "off" register. Blanking sets the data type to IMAGE — where the
value register is a bitmask of lit segments — and writes a mask of zero, so
the panel goes genuinely dark rather than showing `0`.

Two independent timeouts are available, and they cover different failures:

- **Blank Timeout** (config, or per-call `timeout`) — the app blanks the sign
  once a value has been up this long. This is the one you normally want.
- **Safe State Timeout** (config, 0-60 s) — the display's *own* failsafe. If it
  hears nothing on the bus for this long it blanks itself. This is what covers
  the app crashing, the Doovit losing power, or the RS485 cable being cut —
  cases where nothing is left running to honour the first timeout. Off by
  default; set it if a stale number on this sign would be misleading.

## Configuration

| Setting | Default | Notes |
|---------|---------|-------|
| Modbus Config | `/dev/ttyAMA0`, 9600 8N1 RTU | The Doovit's RS485 port |
| Slave ID | 1 | The display's factory address |
| Default Colour | green | Used when a call names no colour |
| Brightness | 75 % | |
| Blank Timeout | 300 s | 0 leaves values up indefinitely |
| Scroll Long Text | on | |
| Blink Period | 1000 ms | |
| Scroll Speed | 200 ms/char | |
| Safe State Timeout | 0 (off) | Display's own comms-loss failsafe, max 60 s |
| Resync Interval | 30 s | Re-asserts display state; recovers a power-cycled panel |
| Swap Word Order / Swap Bytes | off | Only for a display with a non-default byte order saved in flash |

### Why there is a resync

Everything this app writes lives in the display's **RAM**. Power-cycle the
panel and it comes back at its saved defaults, showing nothing useful. Rather
than trusting that a successful write stays applied, the app re-asserts the
full display state every `resync_interval` seconds, so a display that dropped
out returns to the right value on its own.

The obvious alternative — writing the config to flash so it survives a power
cycle — is a trap. Flash is committed by writing register 5000, and the part
has a finite write budget (the display reports what is left of it, surfaced as
the `flash_cycles_remaining` tag). An app that saved on every update
would wear the display out. **This app never writes register 5000.**

### Byte order

The defaults are correct for a factory-default display and were confirmed on
hardware: 32-bit values go most-significant word first, and strings pack two
characters per register with the first character in the high byte. The two
swap options exist only for a display previously commissioned with a
non-default **Byte order** parameter saved in its flash — cheaper than
re-flashing the display to run this app. Symptoms: numbers appear as wild or
tiny values (try Swap Word Order), or text renders as scrambled character
pairs (try Swap Bytes).

## Wiring

RS485 A/B to the Doovit's RS485 port (`/dev/ttyAMA0`). If the display does not
respond to a bus scan, **swap A and B** — that and unpowered display output are
the two usual causes. The display is a Modbus slave at address 1 out of the
box, 9600 8N1.

## Development

```bash
uv run pytest tests -v   # unit tests
uv run export-config     # regenerate the config schema locally
```

The encoding tests assert against values read back from a physical SMI2-M, not
just from the manual — see the docstring in `tests/test_driver.py`.
