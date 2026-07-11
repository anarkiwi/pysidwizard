"""Register log generation and serialization (the shared py* convention)."""

from __future__ import annotations

import io

import pytest
from pysidtracker.errors import SidParseError
from pysidtracker.reglog import SID_REG_COUNT

from pysidwizard import (
    End,
    Instrument,
    Pattern,
    PlayPattern,
    Row,
    SWMFile,
    Waveform,
    straight_tempo,
)
from pysidwizard.player import PAL_CYCLES_PER_FRAME, SWMPlayer
from pysidwizard.reglog import (
    RegWrite,
    iter_register_writes,
    read_reglog,
    write_reglog,
)


def _toy_swm() -> SWMFile:
    """A small self-contained module (no network fetch) exercising the
    per-frame note / instrument / table code paths."""
    inst = Instrument(
        name=b"TEST    ",
        attack=0,
        decay=0,
        sustain=0xF,
        release=0,
        first_waveform=Waveform.PULSE | 0x01,
        wf_table=bytes([0x41, 0x80, 0xFF]),
        pw_table=b"\x88\x00\x00",
        filter_table=b"",
    )
    pattern = Pattern(
        rows=[
            Row(note=49, instrument=1),
            Row(),
            Row(note=0x7E),
            Row(note=53, instrument=1),
        ],
        length=4,
    )
    return SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(2), straight_tempo(2))],
    )


def test_clock_layout():
    """Frames are ``cycles_per_frame`` apart; writes within a frame are
    spaced ``write_spacing`` cycles from the frame boundary."""
    song = _toy_swm()
    writes = list(iter_register_writes(song, max_frames=3))
    assert writes, "player produced no register writes"
    assert all(0 <= w.reg <= 0x18 for w in writes)
    assert all(0 <= w.val <= 0xFF for w in writes)
    clocks = [w.clock for w in writes]
    assert clocks == sorted(clocks)
    # The shared framer emits the post-init baseline at frame 0, then the
    # three requested play frames at frames 1..3.
    frame_starts = {w.clock - (w.clock % PAL_CYCLES_PER_FRAME) for w in writes}
    assert frame_starts <= {i * PAL_CYCLES_PER_FRAME for i in range(4)}


def test_matches_play_frame():
    """``iter_register_writes`` yields the shared MemPlayer register stream:
    the post-init baseline, then each frame's changed ``0..0x18`` writes."""
    player = SWMPlayer(_toy_swm())
    expected = list(enumerate(player.regs))  # frame-0 baseline
    for _frame in range(5):
        expected.extend(player.play_frame())
    got = [(w.reg, w.val) for w in iter_register_writes(_toy_swm(), max_frames=5)]
    assert got == expected


def test_clock_options():
    writes = list(
        iter_register_writes(_toy_swm(), max_frames=2, cycles_per_frame=1000, write_spacing=2)
    )
    assert writes[0].clock == 0
    if len(writes) > 1:
        assert writes[1].clock == 2


def test_player_override_is_used():
    """A caller-supplied player is driven instead of building a fresh one."""
    player = SWMPlayer(_toy_swm())
    player.play_frame()  # advance one frame
    writes = list(iter_register_writes(_toy_swm(), max_frames=1, player=player))
    # frame 0 here is actually the player's SECOND frame; just confirm it ran.
    assert writes


def test_bad_spacing():
    with pytest.raises(SidParseError, match="write_spacing"):
        list(iter_register_writes(_toy_swm(), max_frames=1, cycles_per_frame=100))


def test_write_read_path_round_trip(tmp_path):
    writes = list(iter_register_writes(_toy_swm(), max_frames=20))
    path = tmp_path / "song.reglog"
    write_reglog(writes, path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# pysidtracker register log")
    assert read_reglog(path) == writes
    assert read_reglog(str(path)) == writes
    assert read_reglog(io.StringIO(text)) == writes
    with pytest.raises(TypeError):
        read_reglog(12345)


def test_write_stream_no_header():
    writes = [RegWrite(0, 24, 15), RegWrite(19656, 4, 0x41)]
    out = io.StringIO()
    write_reglog(writes, out, header=False)
    assert out.getvalue() == "0 24 15\n19656 4 65\n"
    assert read_reglog(io.StringIO(out.getvalue())) == writes


def test_read_blank_lines_and_comments():
    text = "# comment\n\n0 24 15  # trailing comment\n"
    assert read_reglog(io.StringIO(text)) == [RegWrite(0, 24, 15)]


@pytest.mark.parametrize("bad", ["0 24", "0 24 15 16", "a b c"])
def test_read_bad_lines(bad):
    with pytest.raises(SidParseError, match="line 1"):
        read_reglog(io.StringIO(bad))


def test_sid_registers_constant():
    assert SID_REG_COUNT == 25
