from pathlib import Path

from pydoover import ui

from .app_tags import SMI2MTags


class SMI2MUI(ui.UI):
    """Operator view: what the panel is showing, and a way to drive it by hand.

    The app's real interface is the RPC surface — other apps call ``set_value``
    to put readings up. These controls exist so a human can commission the sign
    and check it from the site page without another app in the loop.
    """

    displayed_value = ui.TextVariable(
        "On Display",
        value=SMI2MTags.displayed_value,
        name="displayed_value",
        position=10,
    )
    displayed_colour = ui.TextVariable(
        "Colour",
        value=SMI2MTags.displayed_colour,
        name="displayed_colour",
        position=11,
    )
    seconds_until_blank = ui.NumericVariable(
        "Blanks In",
        value=SMI2MTags.seconds_until_blank,
        name="seconds_until_blank",
        precision=0,
        position=12,
    )

    manual_message = ui.TextInput(
        "Show A Message",
        name="manual_message",
        position=20,
    )
    manual_colour = ui.Select(
        "Set Colour",
        name="manual_colour",
        position=21,
        options=[
            ui.Option("Green"),
            ui.Option("Red"),
            ui.Option("Yellow"),
        ],
    )
    blank_now = ui.Button(
        "Blank Display",
        name="blank_now",
        position=22,
    )

    diagnostics = ui.Submodule(
        "Diagnostics",
        name="diagnostics",
        position=90,
        children=[
            ui.BooleanVariable(
                "Modbus OK",
                value=SMI2MTags.comms_ok,
                name="comms_ok",
            ),
            ui.TextVariable(
                "Last Error",
                value=SMI2MTags.last_error,
                name="last_error",
            ),
            ui.NumericVariable(
                "Flash Cycles Remaining",
                value=SMI2MTags.flash_cycles_remaining,
                name="flash_cycles_remaining",
                precision=0,
            ),
        ],
    )


def export():
    SMI2MUI(None, None, None).export(
        Path(__file__).parents[2] / "doover_config.json",
        "smi2m_display",
    )


if __name__ == "__main__":
    export()
