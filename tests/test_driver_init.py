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
    SWMPlayer,
    parse_sid,
    parse_sid_song,
    playspec,
    straight_tempo,
)
from pysidwizard.features import detect_quirks
from pysidwizard.formats import FORMAT
from pysidwizard.sidreader import _player_bases, _subtune_operands
from tests._hvsc_fetch import resolve
from tests._toy import assert_6502, labels, note_word


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


def _pitch(row, voice: int = 0) -> int:
    """The frequency word of ``voice`` in register row ``row``."""
    base = voice * playspec.VOICE_STRIDE
    return row[base] | row[base + 1] << 8


def test_setup_ticks_walk_instrument_one_header_from_offset_zero():
    """HRGTOFF sets PTNGATE before HRENDER walks INSPTR[0] (= instrument 1)."""
    player = assert_6502(_swm(bytes([0x1A, 0x0F, 0x00])), 3)
    grid = _grid(player.swm, 2)
    # Row 0 of the header: waveform $1A & $FE, arp $0F, detune $00; HR-AD $0F.
    assert _pitch(grid[0]) == note_word(player, 15)
    assert (grid[0][4], grid[0][5], grid[0][6]) == (0x1A, 0x0F, 0x00)


def test_setup_tick_without_hr_leaves_initers_zero_gate():
    """Control bit 1 clear: no HRGTOFF at TICK_0, so PTNGATE is still INITER's $00."""
    grid = _grid(_swm(bytes([0x19, 0x05, 0x00])), 2)
    assert grid[0][4] == 0x00
    assert grid[1][4] == 0x00 and grid[1][5] == 0x05  # TICK_1 fires HR (bit 0)


def test_first_call_of_a_multispeed_tune_is_mulply():
    """CIAPLAY counts from 0, so calls 1..N-1 are MULPLY and call N is PLAYER.

    MULCNTP never tests CURINS and INITER leaves ARPSCNT at 0, so the first
    (MULPLY) call already walks instrument 0 on every voice; PLAYER runs second.
    """
    player = assert_6502(_swm(bytes([0x1A, 0x0F, 0x00]), frame_speed=2), 3)
    grid = _grid(player.swm, 2)
    for voice in range(playspec.CHANNELS):
        assert _pitch(grid[0], voice) == note_word(player, 15)
        assert grid[0][voice * playspec.VOICE_STRIDE + 4] == 0x00  # PTNGATE still 0
    # PLAYER TICK_0: voice 1's HR walk reaches pitch 0; CURINS=0 voices keep the MULPLY pitch.
    assert _pitch(grid[1]) == note_word(player, 0) and grid[1][5] == 0x0F
    assert _pitch(grid[1], 1) == note_word(player, 15)


def _two_note_swm(arp: int, chord: int) -> SWMFile:
    """Notes 49 then 61, the instrument's one WF row paced by ARPSPED 5."""
    inst = Instrument(
        control=0x1A,
        hr_decay=0xF,
        wf_table=bytes([0x41, arp, 0x00]),
        arp_speed=5,
        default_chord=chord,
    )
    rows = [Row(note=49, instrument=1), Row(), Row(note=61, instrument=1), Row()]
    return SWMFile(
        sequences=[[PlayPattern(1), End()], [End()], [End()]],
        patterns=[Pattern(rows=rows, length=4)],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
    )


@pytest.mark.parametrize("chord", [0, 2])
def test_a_chord_row_writes_the_chord_tables_pitch(chord):
    """A $7F arp row plays CHDT1[CHDP1[chord]] through RELPTCH for any CURCHORD, one the
    module never defines too, so the new note's pitch is not held; RELPTCH's carry
    joins WRPITCH's ADC."""
    player = assert_6502(_two_note_swm(0x7F, chord), 11)
    lab, mem = labels(player), player.memory
    step = mem[lab["p_chdt1"] + mem[lab["p_chdp1"] + chord]]
    assert step not in (0x7E, 0x7F)
    pitch = step + 61
    expect = note_word(player, pitch & 0x7F) + (pitch >> 8)
    assert _pitch(_grid(player.swm, 10)[9]) == expect


@pytest.mark.parametrize(
    "byte0, arp, light, expect_ctrl, expect_note",
    [
        (0x00, 0x7F, False, None, None),  # ARPSPEED row + chord: no waveform, a chord step
        (0x00, 0x7F, True, 0x00, 0x7F),  # Light: $00 is a waveform, $7F a step up
        (0x41, 0x80, True, 0x40, 15),  # Light keeps WFARP_NOP: pitch held
    ],
)
def test_wf_header_row_follows_the_build_flags(byte0, arp, light, expect_ctrl, expect_note):
    """Header row 1 under each build; a chord row steps by CHDT1[0] (INITER's CHORDPOS)."""
    swm = _swm(bytes([0x1A, 0x0F, 0x00]), driver=2 if light else 0)
    if light:
        swm.player_version = "1.7"
    swm.instruments[0].attack, swm.instruments[0].decay = byte0 >> 4, byte0 & 0xF
    swm.instruments[0].sustain, swm.instruments[0].release = arp >> 4, arp & 0xF
    player = assert_6502(swm, 2)
    frame = _grid(swm, 2)[1]
    if expect_ctrl is None:
        step = player.memory[labels(player)["p_chdt1"]]
        assert step not in (0x7E, 0x7F)
        assert frame[4] == 0x1A and _pitch(frame) == note_word(player, step & 0x7F)
    else:
        assert frame[4] == expect_ctrl and _pitch(frame) == note_word(player, expect_note)


def test_bare_build_reads_arp_80_as_absolute_pitch_zero():
    swm = _swm(bytes([0x1A, 0x0F, 0x00]), driver=4)
    swm.instruments[0].attack, swm.instruments[0].decay = 4, 1
    swm.instruments[0].sustain, swm.instruments[0].release = 8, 0
    player = assert_6502(swm, 2)
    assert _pitch(_grid(swm, 2)[1]) == note_word(player, 0)


@pytest.mark.parametrize("version, by_track", [("1.7", True), ("1.8", False), ("1.97", False)])
def test_pre_18_first_frame_pitch_indexes_freqtbh_by_track(version, by_track):
    """ChangeLog.txt:101: STRTSND's ``lda FREQTBH,x`` typo before SID-Wizard 1.8."""
    swm = _swm(bytes([0x1A, 0x0F, 0x00]))
    swm.player_version = version
    player = assert_6502(swm, 3)
    assert _grid(swm, 3)[2][1] == note_word(player, 0 if by_track else 49) >> 8


@pytest.mark.parametrize("opcode, by_track", [(0xB9, False), (0xBD, True), (None, False)])
def test_first_frame_freq_index_is_read_from_the_player_code(opcode, by_track):
    code = b"" if opcode is None else bytes([0x29, 0x08, 0xF0, 0x10, 0xBC, 0x4A, 0x10, opcode])
    quirks = detect_quirks(b"", b"\x00" * 8 + code + b"\x00" * 8)
    assert quirks.first_frame_freq_by_track is by_track
    assert detect_quirks(code, b"").first_frame_freq_by_track is False


def test_player_bases_need_a_jump_pair_below_the_tracker_text():
    mem = bytearray(0x100)
    mem[0x10:0x16] = bytes([0x4C, 0x30, 0, 0x4C, 0x30, 0])  # INITER past the text
    mem[0x20:0x2A] = b"SID-WIZARD"
    mem[0x80:0x8A] = b"SID-WIZARD"  # no jump table within reach
    assert _player_bases(mem) == [0x10]


def test_ntsc_export_plays_its_own_frequency_table():
    pal = SWMPlayer(parse_sid(_tune("DEMOS/0-9/8-Bit_Bard.sid")))
    ntsc = assert_6502(parse_sid(_tune("DEMOS/A-F/Fallen_Down_Reprise.sid")), 20)
    assert [note_word(ntsc, n) for n in range(96)] != [note_word(pal, n) for n in range(96)]


def test_song_runs_init_to_pick_the_subtune():
    """PREX's init maps songs 1-3 to subtunes 1-3 of the player at $1000."""
    data = _tune("GAMES/M-R/PREX.sid")
    whole = parse_sid(data)
    for song in (1, 2, 3):
        assert parse_sid_song(data, song).sequences == whole.subtune(song).sequences


def test_format_plays_the_selected_subtune():
    """The format's ``subtune`` is the 0-based song: PREX's song 4 is its second player."""
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


def test_song_beyond_the_decoded_subtunes_is_a_parse_error(monkeypatch):
    data = _tune("DEMOS/0-9/8-Bit_Bard.sid")
    assert parse_sid_song(data, 1).sequences == parse_sid(data).sequences[:3]
    with pytest.raises(SIDFormatError, match="selects subtune"):
        parse_sid_song(data, 2)
    monkeypatch.setattr("pysidwizard.sidreader._song_entry", lambda *_: (None, b""))
    with pytest.raises(SIDFormatError, match="selects subtune 1"):
        parse_sid_song(data, 2)


def test_gated_player_ticks_on_alternate_calls():
    """Kauas pilvet's hand-edited gate in front of PLAYER skips every other call, the first
    included; a skipped call leaves the registers be. 8-Bit Bard has no gate."""
    swm = parse_sid_song(_tune("MUSICIANS/D/Deetsay/Kauas_pilvet_scrollaavat.sid"), 1)
    assert labels(assert_6502(swm, 40))["gate"]
    grid = _grid(swm, 40)
    changed = {k for k in range(1, 40) if grid[k] != grid[k - 1]}
    assert changed and all(k % 2 for k in changed)
    assert not labels(SWMPlayer(parse_sid(_tune("DEMOS/0-9/8-Bit_Bard.sid"))))["gate"]


@pytest.mark.parametrize(
    "tail, rewinds",
    [(bytes([0xEC, 0x97, 0x14, 0xD0, 0x03]), True), (bytes([0xBD, 0x88, 0x10]), False)],
)
def test_stopflt_is_read_from_the_player_code(tail, rewinds):
    """SETFLTP's ``bne wriFswi`` falls into STOPFLT's ``cpx FLTCTRL`` (player.asm:2003), 1.7 on."""
    setfltp = bytes.fromhex("c9fff00e8e97148c9b14bd87100d0711d006")
    assert detect_quirks(b"\x00" * 8 + setfltp + tail, b"").filter_stop_rewinds is rewinds
    assert detect_quirks(b"\x00" * 8, b"").filter_stop_rewinds is True


def test_parse_sid_reads_stopflt_from_the_entered_player():
    """A pre-1.7 export's SETFLTP leaves FLTPOSI walking past an $FF-first filter table."""
    data = _tune("MUSICIANS/S/Sta64/Funkito_Reloaded.sid")
    assert parse_sid_song(data, 1).player.quirks.filter_stop_rewinds is False


@pytest.mark.parametrize(
    "code, base", [("69 00 a8 b9 34 12", 0x1234), ("0a a8 a2 01 b9 cf b5", 0xB5CF)]
)
def test_subtunes_base_is_read_from_either_setstune_form(code, base):
    """1.2+ SETSEQA's ``adc #SUBTPOS ; tay`` or V1.0 SETSTUNE's ``asl ; tay ; ldx #1``."""
    assert _subtune_operands(bytes(8) + bytes.fromhex(code) + bytes(8)) == [base]


def test_v10_player_reads_every_subtune_from_its_setstune_table():
    """Sheepoid DX's V1.0 SETSTUNE indexes SUBTUNES by ``asl ; asl ; asl``: song 1 is subtune 0."""
    data = _tune("MUSICIANS/B/Bayliss_Richard/Sheepoid_DX.sid")
    assert parse_sid(data).subtune_count == 10
    swm = parse_sid_song(data, 1)
    assert [seq[0] for seq in swm.sequences] == [PlayPattern(10), PlayPattern(11), PlayPattern(12)]
    assert swm.player.quirks.first_frame_fixed_waveform is True
    bard = parse_sid_song(_tune("DEMOS/0-9/8-Bit_Bard.sid"), 1)
    assert bard.player.quirks.first_frame_fixed_waveform is False


def test_new_note_ticks_leave_the_init_pulse_width():
    """HRENDER's ``jmp WFARPTB`` and STRTSND skip SETPWID: the chip keeps INITER's clear."""
    swm = _swm(bytes([0x1A, 0x0F, 0x00]))
    swm.instruments[0].pw_table = bytes([0x84, 0x40, 0x00])
    assert_6502(swm, 4)
    grid = _grid(swm, 4)
    assert [row[2:4] for row in grid] == [[0x00, 0x00]] * 3 + [[0x40, 0x04]]
