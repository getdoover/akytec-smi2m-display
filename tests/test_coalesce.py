"""Tests for the register-write coalescer.

At 9600 baud every extra Modbus transaction is real latency, and the display's
registers are laid out so that related settings sit next to each other. The
coalescer is what turns "colour changed and so did the value" into the fewest
possible writes.
"""

from smi2m_display.application import SMI2MApplication
from smi2m_display.smi2m_driver import (
    REG_SAFE_STATE_BITMASK,
    REG_SAFE_STATE_BLINKING,
    REG_SAFE_STATE_COLOUR,
    REG_SAFE_STATE_TIMEOUT,
)

coalesce = SMI2MApplication._coalesce


class TestCoalesce:
    def test_merges_adjacent_runs(self):
        assert coalesce({10: [1, 2], 12: [3]}) == [(10, [1, 2, 3])]

    def test_keeps_disjoint_runs_apart(self):
        assert coalesce({10: [1], 20: [2]}) == [(10, [1]), (20, [2])]

    def test_orders_by_address_regardless_of_insertion_order(self):
        assert coalesce({20: [2], 10: [1]}) == [(10, [1]), (20, [2])]

    def test_merges_a_chain_of_runs_up_to_a_gap(self):
        # 4, 5, then 6..7 chain into one run; 9 is past the end of it.
        assert coalesce({4: [1], 5: [2], 6: [3, 4], 9: [5]}) == [
            (4, [1, 2, 3, 4]),
            (9, [5]),
        ]

    def test_gap_of_one_is_not_merged(self):
        # Writing across a gap would clobber the register in between.
        assert coalesce({10: [1], 12: [2]}) == [(10, [1]), (12, [2])]

    def test_safe_state_registers_collapse_to_one_write(self):
        # 4062 timeout, 4063..4064 bitmask, 4065 colour, 4066 blinking.
        blocks = {
            REG_SAFE_STATE_TIMEOUT: [30],
            REG_SAFE_STATE_BITMASK: [0, 0],
            REG_SAFE_STATE_COLOUR: [1],
            REG_SAFE_STATE_BLINKING: [0],
        }
        assert coalesce(blocks) == [(REG_SAFE_STATE_TIMEOUT, [30, 0, 0, 1, 0])]

    def test_does_not_mutate_input(self):
        original = [1, 2]
        blocks = {10: original, 12: [3]}
        coalesce(blocks)
        assert original == [1, 2]

    def test_empty(self):
        assert coalesce({}) == []
