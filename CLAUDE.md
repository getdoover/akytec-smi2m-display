# SMI2-M Display

Doover device app driving an akYtec SMI2-M RS485 7-segment display over Modbus
RTU, built on pydoover 1.0. See `README.md` for the RPC interface and config.

## Commands

```bash
uv run pytest tests -v   # tests
uv run export-config     # regenerate the config schema locally (CI generates it at publish)
```

## Layout

```
src/smi2m_display/
  __init__.py        # entry point — run_app(SMI2MApplication())
  smi2m_driver.py    # register map + encoders; no I/O, no pydoover
  application.py     # loop, Modbus writes, RPC handlers
  app_config.py      # config schema
  app_tags.py        # runtime tags (telemetry only — this app has no UI)
```

`smi2m_driver.py` is deliberately pure — pure functions over ints and strings —
so the encoding is unit-testable without a bus or a device agent. All I/O lives
in `application.py`.

## Things worth knowing before changing this

- **Never write register 5000 (Save-to-Flash).** Display config is RAM-only by
  design here; flash has a finite write budget. The resync loop, not flash,
  is what survives a display power cycle.
- **The display has no timer.** `countdown` is driven entirely by the app's
  1 Hz loop; data type 7 (`TIME`, register 4252) only *formats* a raw second
  count as MM:SS, capped at 5999 s (99:59) before the panel shows `ErrH`.
  Firing the RPC once saves messages, not bus traffic.
- **Blanking is an IMAGE write of bitmask 0**, not a value write — there is no
  off register, and writing `0` would show a literal zero.
- **Strings are always 16 registers.** In SLAVE mode the display rejects
  partial string reads/writes with Modbus exception 2 (confirmed on hardware).
  Short messages are space-padded to the full 32 characters.
- **Register writes go through `_coalesce`**, which merges adjacent runs into
  single transactions. Add registers as named entries in the desired-state
  dict and let it do the merging; don't hand-roll offset arithmetic.
- **Desired vs written state.** `_desired` is what the panel should show;
  `_written` is what we last confirmed. Only the diff goes on the bus, which
  is what keeps a 1 Hz loop from saturating a 9600 baud line.

## Hardware notes

The display sits at slave address 1, 9600 8N1, on `/dev/ttyAMA0` (the Doovit's
RS485 port). If a bus scan finds nothing, check the display is powered and try
swapping A/B. Encoding expectations in `tests/test_driver.py` were verified
against a physical unit — treat them as hardware facts, not guesses.

## No UI

This is a machine-only app: it is driven by other apps over RPC, not by a
person on the site page. There is no `app_ui.py` and no `ui_cls`; `Application`
defaults `ui_cls` to the empty base `UI`. Tags are still published — they are
the telemetry/alarm surface, independent of any UI.

RPC handlers are registered on `rpc.DEFAULT_CHANNEL` (`"dv-rpc"`), the channel
`rpc.call()` targets by default. **The channel must be named explicitly**:
`register_handlers` only subscribes to a channel that is stated, so a handler
left at `channel=None` is registered as a global handler and then never hears
anything, because nothing else subscribes this app to `dv-rpc`.
