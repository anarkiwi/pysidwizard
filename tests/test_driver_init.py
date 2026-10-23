"""INITER state, song selection and build gates the player and reader model."""

# pylint: disable=missing-function-docstring

import pytest

from pysidwizard import (
    End,
    Instrument,
    Pattern,
    PlayPattern,
    Row,
    SIDFormatError,
    SWMFile,
    parse_sid,
    parse_sid_song,
    straight_tempo,
)
from pysidwizard.features import features_for_driver
from pysidwizard.formats import FORMAT
from pysidwizard.player import NOTE_FREQ_HI, NOTE_FREQ_LO, TRACK_STRIDE, SWMPlayer, freq_tables
from pysidwizard.sidreader import _first_frame_freq_by_track, _player_bases
from tests._hvsc_fetch import resolve


def _tune(relpath):
    path = resolve(relpath)
    if path is None:
        pytest.skip(f"{relpath} unreachable (offline)")
    return path.read_bytes()


def _swm(header: bytes, *, driver: int = 0, frame_speed: int = 1, row=None) -> SWMFile:
    """One voice, one note row at tempo 3; instrument 1's header is ``header``."""
    inst = Instrument(
        control=header[0],
        hr_attack=header[1] >> 4,
        hr_decay=header[1] & 0xF,
        hr_sustain=header[2] >> 4,
        hr_release=header[2] & 0xF,
        first_waveform=0x09,
        wf_table=bytes([0x41, 0x80, 0xFF]),
    )
    pattern = Pattern(rows=[row or Row(note=49, instrument=1), Row()], length=2)
    return SWMFile(
        sequences=[[PlayPattern(1), End()], [End()], [End()]],
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
        driver_type=driver,
        frame_speed=frame_speed,
    )


def _grid(swm: SWMFile, frames: int):
    return SWMPlayer(swm).render_grid(frames)


def test_setup_ticks_walk_instrument_one_header_from_offset_zero():
    """HRGTOFF sets PTNGATE before HRENDER walks INSPTR[0] (= instrument 1)."""
    grid = _grid(_swm(bytes([0x1A, 0x0F, 0x00])), 2)
    # Row 0 of the header: waveform $1A & $FE, arp $0F, detune $00; HR-AD $0F.
    assert grid[0][:2] == [NOTE_FREQ_LO[15], NOTE_FREQ_HI[15]]
    assert (grid[0][4], grid[0][5], grid[0][6]) == (0x1A, 0x0F, 0x00)


def test_setup_tick_without_hr_leaves_initers_zero_gate():
    """Control bit 1 clear: no HRGTOFF at TICK_0, so PTNGATE is still INITER's $00."""
    grid = _grid(_swm(bytes([0x19, 0x05, 0x00])), 2)
    assert grid[0][4] == 0x00
    assert grid[1][4] == 0x00 and grid[1][5] == 0x05  # TICK_1 fires HR (bit 0)


def test_first_call_of_a_multispeed_tune_is_mulply():
    """CIAPLAY counts from 0, so calls 1..N-1 are MULPLY and call N is PLAYER.

    MULCNTP never tests CURINS and INITER leaves ARPSCNT at 0, so the first
    (MULPLY) call already walks instrument 0 on every voice; PLAYER only runs
    on the second call.
    """
    swm = _swm(bytes([0x1A, 0x0F, 0x00]), frame_speed=2)
    grid = _grid(swm, 2)
    for base in (0, TRACK_STRIDE, 2 * TRACK_STRIDE):
        assert grid[0][base : base + 2] == [NOTE_FREQ_LO[15], NOTE_FREQ_HI[15]]
        assert grid[0][base + 4] == 0x00  # PTNGATE still 0: no HRGTOFF yet
    # PLAYER TICK_0: HR on voice 1 and its walk reaches offset 3 (pitch 0);
    # voices 2 and 3 take CNTPLAY's CURINS=0 return and keep the MULPLY pitch.
    assert grid[1][:2] == [NOTE_FREQ_LO[0], NOTE_FREQ_HI[0]] and grid[1][5] == 0x0F
    assert grid[1][TRACK_STRIDE : TRACK_STRIDE + 2] == [NOTE_FREQ_LO[15], NOTE_FREQ_HI[15]]


def test_init_register_state_is_where_the_first_call_starts():
    swm = _swm(bytes([0x1A, 0x0F, 0x00]), row=Row())
    swm.sid_init = bytes([0x08] * 25)
    frame = _grid(swm, 1)[0]
    assert frame[:0x15] == [0x08] * 0x15
    assert frame[0x18] == 0x0F  # COMMONREGS writes $D418 on every call


def test_filter_operands_the_init_leaves_reach_the_first_call():
    """RESONIB / CTFHGHO / CTFLGHO / FSWITCH as the init left them (player.asm:749-797)."""
    swm = _swm(bytes([0x1A, 0x0F, 0x00]), row=Row())
    swm.filter_init = (0xA0, 0x0B, 0x07, 0x01)
    assert _grid(swm, 1)[0][0x15:0x18] == [0x07, 0x0B, 0xA1]
    assert _grid(swm.subtune(0), 1)[0][0x15:0x18] == [0x07, 0x0B, 0xA1]


@pytest.mark.parametrize(
    "relpath, song, image, expect",
    [
        ("MUSICIANS/M/Mario64/Club64.sid", 1, 0x07, 0x00),  # INITER zeroes it
        (
            "MUSICIANS/N/NecroPolo/Lights_On.sid",
            2,
            0x00,
            0x01,
        ),  # the $2000 player's image holds $01
    ],
)
def test_fswitch_is_the_operand_the_init_leaves(relpath, song, image, expect):
    """FSWITCH (player.asm:750) is read after the init, which zeroes it only under FILT_CTRL_FX_ON."""
    data = _tune(relpath)
    assert parse_sid(data).filter_init[3] == image
    assert parse_sid_song(data, song).filter_init[3] == expect


@pytest.mark.parametrize(
    "byte0, arp, light, expect_ctrl, expect_note",
    [
        (0x00, 0x7F, False, None, None),  # ARPSPEED row + chord: no waveform, no pitch
        (0x00, 0x7F, True, 0x00, 0x7F),  # Light: $00 is a waveform, $7F a step up
        (0x41, 0x80, True, 0x40, 15),  # Light keeps WFARP_NOP: pitch held
    ],
)
def test_wf_header_row_follows_the_build_flags(byte0, arp, light, expect_ctrl, expect_note):
    driver = 2 if light else 0
    swm = _swm(bytes([0x1A, 0x0F, 0x00]), driver=driver)
    swm.instruments[0].attack, swm.instruments[0].decay = byte0 >> 4, byte0 & 0xF
    swm.instruments[0].sustain, swm.instruments[0].release = arp >> 4, arp & 0xF
    lo, hi = freq_tables(features_for_driver(driver))
    frame = _grid(swm, 2)[1]
    if expect_ctrl is None:
        assert frame[4] == 0x1A and frame[:2] == [NOTE_FREQ_LO[15], NOTE_FREQ_HI[15]]
    else:
        assert frame[4] == expect_ctrl and frame[:2] == [lo[expect_note], hi[expect_note]]


def test_bare_build_reads_arp_80_as_absolute_pitch_zero():
    swm = _swm(bytes([0x1A, 0x0F, 0x00]), driver=4)
    swm.instruments[0].attack, swm.instruments[0].decay = 4, 1
    swm.instruments[0].sustain, swm.instruments[0].release = 8, 0
    lo, hi = freq_tables(features_for_driver(4))
    assert _grid(swm, 2)[1][:2] == [lo[0], hi[0]]


def test_pre_18_first_frame_pitch_indexes_freqtbh_by_track():
    """ChangeLog.txt:101: STRTSND's ``lda FREQTBH,x`` typo before SID-Wizard 1.8."""
    swm = _swm(bytes([0x1A, 0x0F, 0x00]))
    assert _grid(swm, 3)[2][1] == NOTE_FREQ_HI[49]
    swm.first_frame_freq_by_track = True
    assert _grid(swm, 3)[2][1] == NOTE_FREQ_HI[0]


@pytest.mark.parametrize("opcode, by_track", [(0xB9, False), (0xBD, True), (None, False)])
def test_first_frame_freq_index_is_read_from_the_player_code(opcode, by_track):
    code = b"" if opcode is None else bytes([0x29, 0x08, 0xF0, 0x10, 0xBC, 0x4A, 0x10, opcode])
    assert _first_frame_freq_by_track(b"\x00" * 8 + code + b"\x00" * 8) is by_track


def test_player_bases_need_a_jump_pair_below_the_tracker_text():
    mem = bytearray(0x100)
    mem[0x10:0x16] = bytes([0x4C, 0, 0, 0x4C, 0, 0])
    mem[0x20:0x2A] = b"SID-WIZARD"
    mem[0x80:0x8A] = b"SID-WIZARD"  # no jump table within reach
    assert _player_bases(mem) == [0x10]


def test_ntsc_export_plays_its_own_frequency_table():
    pal = parse_sid(_tune("DEMOS/0-9/8-Bit_Bard.sid"))
    ntsc = parse_sid(_tune("DEMOS/A-F/Fallen_Down_Reprise.sid"))
    assert pal.freq_lo_image[:96] == NOTE_FREQ_LO and pal.freq_hi_image[:96] == NOTE_FREQ_HI
    assert ntsc.freq_lo_image[:96] != NOTE_FREQ_LO
    assert SWMPlayer(ntsc).freq_lo == ntsc.freq_lo_image


def test_song_runs_init_to_pick_the_subtune():
    """PREX's init maps songs 1-3 to subtunes 1-3 of the player at $1000."""
    data = _tune("GAMES/M-R/PREX.sid")
    whole = parse_sid(data)
    for song in (1, 2, 3):
        assert parse_sid_song(data, song).sequences == whole.subtune(song).sequences


def test_format_plays_the_selected_subtune():
    """The registered format's ``subtune`` is the 0-based song: PREX's song 4 is its second player."""
    data = _tune("GAMES/M-R/PREX.sid")
    model = FORMAT.parser.parse(data, subtune=3)
    assert model.sequences == parse_sid_song(data, 4).sequences
    assert FORMAT.parser.parse(data).sequences == parse_sid(data).sequences
    expected = SWMPlayer(parse_sid_song(data, 4)).render_grid(200)
    assert FORMAT.player(model).render_grid(200) == expected
    assert expected != SWMPlayer(parse_sid_song(data, 1)).render_grid(200)


def test_song_selects_the_module_of_the_player_it_enters():
    """PREX songs 4-9 run a second player at $3000 with its own (Light) module."""
    data = _tune("GAMES/M-R/PREX.sid")
    first, second = parse_sid_song(data, 1), parse_sid_song(data, 4)
    assert (first.driver_type, second.driver_type) == (3, 2)
    assert second.patterns != first.patterns


def test_song_selects_the_packed_module_init_passes_in_x_y():
    """Immensity's init hands the SWP player a different module base per song."""
    data = _tune("GAMES/G-L/Immensity_preview_2.sid")
    songs = [parse_sid_song(data, song) for song in (1, 2, 3, 4)]
    assert len({len(s.patterns) for s in songs}) > 1
    assert all(s.subtune_count == 1 for s in songs)


def test_song_follows_a_patched_jump_table():
    """Awakening's ``inisub`` adds 1 before INITER, so song n plays subtune n."""
    data = _tune("MUSICIANS/S/Spider_Jerusalem/Awakening.sid")
    whole = parse_sid(data)
    assert parse_sid_song(data, 1).sequences == whole.subtune(1).sequences


def test_song_reports_init_sid_writes():
    """An early INITER clears the SID with the test bit ($08) rather than zero."""
    swm = parse_sid_song(_tune("MUSICIANS/A/Ant1/Techno_2.sid"), 1)
    assert swm.sid_init[:0x18] == bytes([0x08] * 0x18)


def test_song_beyond_the_decoded_subtunes_is_a_parse_error(monkeypatch):
    data = _tune("DEMOS/0-9/8-Bit_Bard.sid")
    assert parse_sid_song(data, 1).sequences == parse_sid(data).sequences[:3]
    with pytest.raises(SIDFormatError, match="selects subtune"):
        parse_sid_song(data, 2)
    monkeypatch.setattr("pysidwizard.sidreader._song_entry", lambda *_: (None, b"", b""))
    with pytest.raises(SIDFormatError, match="selects subtune 1"):
        parse_sid_song(data, 2)
