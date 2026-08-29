"""Driver unit tests.

The encoding expectations here are not inferred from the manual alone — they
were confirmed against a physical SMI2-M on a Doovit at 9600 8N1, slave 1, by
writing each value and reading it back over the same bus.
"""

import pytest

from smi2m_display.smi2m_driver import (
    DISPLAY_MAX,
    DISPLAY_MIN,
    STRING_REGISTERS,
    TIME_MAX_SECONDS,
    Colour,
    DataType,
    DisplayMode,
    decimal_point_for,
    float_to_registers,
    format_time,
    integer_digits,
    parse_colour,
    sanitise_string,
    string_to_registers,
    time_fits,
    uint32_to_registers,
    value_fits,
)


class TestFloatEncoding:
    def test_matches_hardware_readback(self):
        # Confirmed on device: writing 123.4 to register 4206 reads back as
        # [17142, 52429], i.e. most-significant word first.
        assert float_to_registers(123.4) == [17142, 52429]

    def test_zero(self):
        assert float_to_registers(0.0) == [0, 0]

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_rejected(self, bad):
        # A NaN sneaking through would be written as a valid-looking register
        # pair and render as garbage on the panel with no error anywhere.
        with pytest.raises(ValueError):
            float_to_registers(bad)


class TestIntegerEncoding:
    def test_uint32_splits_high_word_first(self):
        assert uint32_to_registers(0xAABBCCDD) == [0xAABB, 0xCCDD]

    def test_uint32_zero_is_the_blank_bitmask(self):
        assert uint32_to_registers(0) == [0, 0]

    def test_uint32_range_enforced(self):
        with pytest.raises(ValueError):
            uint32_to_registers(0x1_0000_0000)


class TestStringEncoding:
    def test_round_trips_as_the_hardware_returned_it(self):
        # Confirmed on device: "PUMP RUNNING" read back space-padded to 32
        # characters, packed two chars per register, first char in the high byte.
        registers, length = string_to_registers("PUMP RUNNING")
        assert length == 12
        assert len(registers) == STRING_REGISTERS
        decoded = "".join(chr((w >> 8) & 0xFF) + chr(w & 0xFF) for w in registers)
        assert decoded == "PUMP RUNNING".ljust(32)

    def test_always_full_width(self):
        # SLAVE mode rejects partial string writes with Modbus exception 2 —
        # observed on hardware when reading only 6 of the 16 registers.
        registers, _ = string_to_registers("OK")
        assert len(registers) == STRING_REGISTERS

    def test_length_clamped_to_documented_minimum(self):
        _, length = string_to_registers("OK")
        assert length == 4

    def test_length_clamped_to_maximum(self):
        _, length = string_to_registers("X" * 100)
        assert length == 32


class TestSanitise:
    def test_passes_displayable_characters(self):
        assert sanitise_string("PUMP 1.2-A") == "PUMP 1.2-A"

    def test_substitutes_dashes(self):
        assert sanitise_string("A_B") == "A-B"

    def test_replaces_unrenderable_rather_than_dropping(self):
        # Dropping would silently close the gap and read as a different word;
        # a visible "-" tells the operator something was lost.
        assert sanitise_string("A°C") == "A-C"

    def test_newlines_become_spaces(self):
        assert sanitise_string("A\nB") == "A B"


class TestDecimalPoint:
    @pytest.mark.parametrize(
        "value,expected_decimals",
        [
            # Every case below is a worked example from the manual
            # (Examples 5 and 6), used here as the oracle for the formula.
            (5.0, 3),  # "5.000"
            (-5.0, 2),  # "-5.00"
            (10.0, 2),  # "10.00"
            (28.38, 2),  # "28.38"
            (-28.39, 1),  # "-28.3"
            (-25.0, 1),  # "-25.0"
        ],
    )
    def test_matches_manual_examples(self, value, expected_decimals):
        assert decimal_point_for(value) == expected_decimals

    def test_four_digit_value_gets_no_decimals(self):
        assert decimal_point_for(9999) == 0

    def test_explicit_request_wins(self):
        assert decimal_point_for(5.0, requested=1) == 1

    def test_request_clamped_to_hardware_range(self):
        assert decimal_point_for(5.0, requested=9) == 3
        assert decimal_point_for(5.0, requested=-2) == 0

    def test_integer_digits(self):
        assert integer_digits(0.4) == 1
        assert integer_digits(-123.9) == 3


class TestRange:
    def test_bounds(self):
        assert value_fits(DISPLAY_MIN)
        assert value_fits(DISPLAY_MAX)
        assert not value_fits(DISPLAY_MAX + 1)
        assert not value_fits(DISPLAY_MIN - 1)


class TestColour:
    def test_names(self):
        assert parse_colour("red") is Colour.RED
        assert parse_colour("GREEN") is Colour.GREEN
        assert parse_colour(" yellow ") is Colour.YELLOW

    def test_amber_maps_to_yellow(self):
        # The panel mixes red+green; there is no separate amber emitter.
        assert parse_colour("amber") is Colour.YELLOW

    def test_raw_enum_value(self):
        assert parse_colour(1) is Colour.RED

    @pytest.mark.parametrize("bad", ["blue", 7, None, True])
    def test_rejects_unsupported(self, bad):
        with pytest.raises(ValueError):
            parse_colour(bad)


class TestRegisterConstants:
    def test_enum_values_match_datasheet(self):
        assert (DataType.INT, DataType.REAL, DataType.STRING, DataType.IMAGE) == (
            0,
            4,
            5,
            6,
        )
        assert (Colour.GREEN, Colour.RED, Colour.YELLOW) == (0, 1, 2)
        assert (
            DisplayMode.STATIC,
            DisplayMode.TEXT_TICKER,
            DisplayMode.NUMBER_TICKER,
        ) == (0, 1, 2)


class TestTimeFormat:
    """MM:SS rendering for data type 7.

    Unlike the rest of this file these expectations come from the manual
    (Table 4.7 footnote 3) rather than from a bench check, because the panel
    does the division itself — we only mirror it for the status tag.
    """

    def test_matches_the_manual_worked_example(self):
        # "If N = 1000, 16:40 is displayed."
        assert format_time(1000) == "16:40"

    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "00:00"),
            (9, "00:09"),
            (60, "01:00"),
            (120, "02:00"),
            (1200, "20:00"),
            (TIME_MAX_SECONDS, "99:59"),
        ],
    )
    def test_renders_by_integer_division(self, seconds, expected):
        assert format_time(seconds) == expected

    def test_range_stops_at_the_panel_limit(self):
        assert time_fits(TIME_MAX_SECONDS)
        # Above this the display shows ErrH rather than wrapping (Table 4.11).
        assert not time_fits(TIME_MAX_SECONDS + 1)
        assert not time_fits(-1)

    @pytest.mark.parametrize("bad", [-1, TIME_MAX_SECONDS + 1])
    def test_rejects_out_of_range(self, bad):
        with pytest.raises(ValueError):
            format_time(bad)
