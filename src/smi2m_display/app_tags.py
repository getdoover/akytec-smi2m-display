from pydoover.tags import Tag, Tags


class SMI2MTags(Tags):
    # What is on the panel right now, rendered the way an operator would read
    # it. Kept as a string so numbers and text messages share one tag.
    displayed_value = Tag("string", default="")
    displayed_colour = Tag("string", default="green")
    is_blank = Tag("boolean", default=True)

    # Seconds until the value times out and the panel blanks. -1 means the
    # value has no expiry; 0 means it is already blank.
    seconds_until_blank = Tag("number", default=0)

    # Bus health. comms_ok goes false on the first failed write and recovers on
    # the next success, so it is a live indicator rather than a latch.
    comms_ok = Tag("boolean", default=False)
    last_error = Tag("string", default="")
    last_write_ts = Tag("number", default=0)

    # Read back from register 61624 — the display's remaining flash write
    # budget, in percent. Only meaningful if something has been saving config
    # to flash; a healthy install of this app leaves it untouched.
    flash_cycles_remaining = Tag("number", default=0)
