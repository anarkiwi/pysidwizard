"""Round-trip and header checks for native multi-SID work files (issue #53).

The SWMS/SWT/SWMQ (2/3/4-SID) layouts parse and re-emit byte-exactly; the
asserted header values were decoded by hand from SID-Wizard 1.97 settings.cfg.
"""

from __future__ import annotations

from pysidwizard import build_swm, parse_swm
from pysidwizard.constants import (
    SWMQ_MAGIC,
    SWMT_MAGIC,
    SWS_MAGIC,
)

_MAGIC_TO_SID = {
    SWS_MAGIC: 2,
    SWMT_MAGIC: 3,
    SWMQ_MAGIC: 4,
}
# name -> (sid, frame_speed, defptn, highlight, driver, tuning, sequences, sid_addr)
EXPECTED_HEADER = {
    "childhood.sws": (2, 1, 0x20, 4, 0, 0, 6, (0,)),
    "egblues.sws": (2, 1, 0x60, 3, 0, 0, 6, (0,)),
    "chb-stickman.sws": (2, 1, 0x20, 4, 0, 0, 6, (0,)),
    "newwave.swt": (3, 1, 0x40, 4, 0, 0, 9, (0, 6)),
    "techno.swt": (3, 1, 0x20, 4, 0, 0, 9, (0, 6)),
    "thizizda.swt": (3, 1, 0x40, 4, 0, 0, 9, (0, 6)),
    "ramos.swt": (3, 1, 0x40, 4, 0, 0, 9, (0, 6)),
    "4sid-example.swq": (4, 1, 0x40, 4, 0, 0, 12, (0, 6, 7)),
}


def _raw_magic(data: bytes) -> bytes:
    """Recover the 4-byte magic independent of the library's layout table."""
    for offset in (0, 2):
        if data[offset : offset + 4][0:3] == b"SWM":
            return data[offset : offset + 4]
    raise AssertionError(f"no SWM-family magic in {data[:8]!r}")


def test_round_trips_byte_exact(multi_sample_bytes: bytes) -> None:
    assert build_swm(parse_swm(multi_sample_bytes)) == multi_sample_bytes


def test_sid_amount_matches_magic(multi_sample_bytes: bytes) -> None:
    expected = _MAGIC_TO_SID[_raw_magic(multi_sample_bytes)]
    assert parse_swm(multi_sample_bytes).sid_amount == expected


def test_channel_grouping_invariants(multi_sample_path, multi_sample_bytes: bytes) -> None:
    swm = parse_swm(multi_sample_bytes)
    channels = 3 * swm.sid_amount
    assert swm.sid_amount >= 2
    assert len(swm.mute) == channels  # per-SID mute block: 3 bytes per chip
    assert swm.sequence_count % channels == 0
    assert swm.subtune_count == len(swm.subtune_tempos)
    assert len(swm.sid_addresses) == swm.sid_amount - 1
    assert swm.subtune_count >= 1 and swm.sequence_count > 0


def test_header_fields_match_hand_decoded(multi_sample_path, multi_sample_bytes: bytes) -> None:
    swm = parse_swm(multi_sample_bytes)
    name = multi_sample_path.name
    sid, frame_speed, defptn, hili, driver, tuning, seq, sid_addr = EXPECTED_HEADER[name]
    assert swm.sid_amount == sid
    assert swm.frame_speed == frame_speed
    assert swm.default_pattern_length == defptn
    assert swm.highlight == hili
    assert swm.driver_type == driver
    assert swm.tuning_type == tuning
    assert swm.sequence_count == seq
    assert tuple(swm.sid_addresses) == sid_addr
