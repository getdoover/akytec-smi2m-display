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

    # -- resync --------------------------------------------------------

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
