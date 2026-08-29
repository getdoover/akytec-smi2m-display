from pydoover.tags import Tag, Tags


class SMI2MTags(Tags):
    # What is on the panel right now, rendered the way an operator would read
    # it. Kept as a string so numbers and text messages share one tag.
    displayed_value = Tag("string", default="")
    displayed_colour = Tag("string", default="green")
    is_blank = Tag("boolean", default=True)

    # Bus health. comms_ok goes false on the first failed write and recovers on
    # the next success, so it is a live indicator rather than a latch.
    comms_ok = Tag("boolean", default=False)
    last_error = Tag("string", default="")
    last_write_ts = Tag("number", default=0)
