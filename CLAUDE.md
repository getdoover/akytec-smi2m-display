# SMI2-M Display

Doover device app driving an akYtec SMI2-M RS485 7-segment display over Modbus
RTU, built on pydoover 1.0. See `README.md` for the RPC interface and config.

## Commands

```bash
uv run pytest tests -v   # tests
uv run export-config     # write config_schema into doover_config.json
uv run export-ui         # write ui_schema into doover_config.json (required to publish)
```

## Layout

```
src/smi2m_display/
  __init__.py        # entry point — run_app(SMI2MApplication())
  smi2m_driver.py    # register map + encoders; no I/O, no pydoover
  application.py     # loop, Modbus writes, RPC + UI handlers
  app_config.py      # config schema
  app_tags.py        # runtime tags
  app_ui.py          # site-page UI
```

`smi2m_driver.py` is deliberately pure — pure functions over ints and strings —
so the encoding is unit-testable without a bus or a device agent. All I/O lives
in `application.py`.

## Things worth knowing before changing this

- **Never write register 5000 (Save-to-Flash).** Display config is RAM-only by
  design here; flash has a finite write budget. The resync loop, not flash,
  is what survives a display power cycle.
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
