from pathlib import Path

from pydoover import config
from pydoover.docker.modbus import ModbusConfig

from .smi2m_driver import DEFAULT_SLAVE_ID


class SMI2MConfig(config.Schema):
    # Default display name "Modbus Config" sanitises to the JSON key
    # "modbus_config", which is what ModbusInterface looks for.
    modbus_config = ModbusConfig()

    slave_id = config.Integer(
        "Slave ID",
        name="slave_id",
        default=DEFAULT_SLAVE_ID,
        minimum=1,
        maximum=255,
        description=(
            "Modbus address of the display (its 'Address in Slave mode' "
            "parameter). Factory default is 1."
        ),
    )

    # -- default presentation ------------------------------------------

    default_colour = config.Enum(
        "Default Colour",
        name="default_colour",
        choices=["green", "red", "yellow"],
        default="green",
        description="Colour used when a set_value call does not specify one.",
    )

    brightness = config.Integer(
        "Brightness",
        name="brightness",
        default=75,
        minimum=0,
        maximum=100,
        description="Panel brightness, in percent.",
    )

    blank_timeout = config.Number(
        "Blank Timeout",
        name="blank_timeout",
        default=300.0,
        minimum=0.0,
        description=(
            "Seconds a value stays on screen before the display blanks itself. "
            "Set 0 to leave values up indefinitely. Individual set_value calls "
            "can override this."
        ),
    )

    scroll_long_text = config.Boolean(
        "Scroll Long Text",
        name="scroll_long_text",
        default=True,
        description=(
            "Show strings longer than 4 characters by scrolling them across "
            "the panel. With this off, only the first 4 characters are shown."
        ),
    )

    blink_period = config.Integer(
        "Blink Period",
        name="blink_period",
        default=1000,
        minimum=250,
        maximum=3000,
        description="Blink period in milliseconds, when blinking is requested.",
    )

    scroll_tick = config.Integer(
        "Scroll Speed",
        name="scroll_tick",
        default=200,
        minimum=100,
        maximum=1500,
        description=(
            "Milliseconds per character step when scrolling. Lower is faster; "
            "200 ms is comfortable to read from a distance."
        ),
    )

    # -- hardware failsafe ---------------------------------------------

    safe_state_timeout = config.Integer(
        "Safe State Timeout",
        name="safe_state_timeout",
        default=0,
        minimum=0,
        maximum=60,
        description=(
            "The display's own comms-loss failsafe: if it receives nothing for "
            "this many seconds it blanks itself, so a dead Doovit cannot leave "
            "a stale number on the panel. Max 60. Applied on startup, and 0 is "
            "written too, so this genuinely disables a failsafe the display "
            "already had stored. Must be LONGER than the resync interval: set "
            "it shorter and the panel falls back to its safe-state pattern "
            "between our writes."
        ),
    )

    # -- advanced ------------------------------------------------------

    swap_words = config.Boolean(
        "Swap 32-bit Word Order",
        name="swap_words",
        default=False,
        description=(
            "Advanced. Only needed if the display has a non-default 'Byte "
            "order' parameter saved in its flash. Symptom: numbers appear as "
            "wild or tiny values."
        ),
    )

    swap_bytes = config.Boolean(
        "Swap Bytes Within Registers",
        name="swap_bytes",
        default=False,
        description=(
            "Advanced. Companion to the above, for a display configured with "
            "byte swapping. Symptom: text renders as scrambled character pairs."
        ),
    )

    resync_interval = config.Number(
        "Resync Interval",
        name="resync_interval",
        default=30.0,
        minimum=0.0,
        description=(
            "Seconds between re-asserting the full display state. This is what "
            "restores the panel after it is power-cycled, since everything this "
            "app writes lives in the display's RAM. 0 disables resyncing."
        ),
    )


def export():
    SMI2MConfig.export(
        Path(__file__).parents[2] / "doover_config.json", "smi2m_display"
    )


if __name__ == "__main__":
    export()
