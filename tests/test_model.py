"""Tests for the high-level :class:`SWMFile`, :class:`Instrument`, and
:class:`Row` / sequence models."""

import pytest

from pysidwizard import (
    End,
    Instrument,
    Loop,
    MainVolume,
    Pattern,
    PlayPattern,
    RawSequenceByte,
    Row,
    SWMFile,
    SWMFormatError,
    TempoOverride,
    Transpose,
    decode_sequence,
    encode_sequence,
)

# --- SWMFile defaults / derived properties ---------------------------------


def test_swmfile_defaults_have_single_subtune_when_empty():
    swm = SWMFile()
    assert swm.sequence_count == 0
    assert swm.subtune_count == 1


def test_swmfile_subtune_count_groups_sequences_by_three():
    swm = SWMFile(sequences=[[End()]] * 3)
    assert swm.subtune_count == 1
    swm.sequences = [[End()]] * 4
    assert swm.subtune_count == 2
    swm.sequences = [[End()]] * 6
    assert swm.subtune_count == 2
    swm.sequences = [[End()]] * 7
    assert swm.subtune_count == 3


def test_swmfile_author_str_strips_padding():
    swm = SWMFile(author=b"hermit                                  ")
    assert swm.author_str() == "hermit"


def test_swmfile_author_bytes_padded_pads_with_spaces_to_header_size():
    swm = SWMFile(author=b"abc")
    padded = swm.author_bytes_padded()
    assert len(padded) == 40
    assert padded.startswith(b"abc")
    assert padded.endswith(b" ")


def test_swmfile_author_bytes_padded_truncates_long_input():
    swm = SWMFile(author=b"x" * 200)
    assert swm.author_bytes_padded() == b"x" * 40


# --- Instrument ---------------------------------------------------------


def test_instrument_name_str_strips_padding():
    ins = Instrument(name=b"DRUM    ")
    assert ins.name_str() == "DRUM"


def test_instrument_name_str_handles_null_padding():
    ins = Instrument(name=b"BASS\x00\x00\x00\x00")
    assert ins.name_str() == "BASS"


def test_instrument_encode_then_decode_round_trips_named_fields():
    ins = Instrument(
        name=b"LEAD    ",
        control=0x1A,
        hr_attack=0,
        hr_decay=0xF,
        hr_sustain=0xF,
        hr_release=0,
        attack=0,
        decay=0,
        sustain=0xF,
        release=0,
        vibrato=0x21,
        vibrato_delay=4,
        arp_speed=2,
        default_chord=1,
        octave_shift=-12,
        gateoff_wf=0,
        gateoff_pw=0,
        gateoff_filt=0,
        first_waveform=0x04,
        wf_table=b"\x41\x00\x00",
        pw_table=b"\x88\x00\x00",
        filter_table=b"\x9f\x03\x01",
    )
    body = ins.encode()
    decoded = Instrument.decode(body, name=b"LEAD    ")
    for attr in (
        "control",
        "hr_attack",
        "hr_decay",
        "hr_sustain",
        "hr_release",
        "attack",
        "decay",
        "sustain",
        "release",
        "vibrato",
        "vibrato_delay",
        "arp_speed",
        "default_chord",
        "octave_shift",
        "gateoff_wf",
        "gateoff_pw",
        "gateoff_filt",
        "first_waveform",
        "wf_table",
        "pw_table",
        "filter_table",
        "name",
    ):
        assert getattr(decoded, attr) == getattr(ins, attr), attr


def test_instrument_octave_shift_round_trips_negative_value():
    ins = Instrument(octave_shift=-1)
    decoded = Instrument.decode(ins.encode(), name=b"")
    assert decoded.octave_shift == -1


def test_instrument_rejects_out_of_range_nibble():
    ins = Instrument(attack=16)
    with pytest.raises(SWMFormatError, match="attack"):
        ins.encode()


def test_instrument_rejects_out_of_range_octave():
    ins = Instrument(octave_shift=200)
    with pytest.raises(SWMFormatError, match="octave_shift"):
        ins.encode()


def test_instrument_decode_rejects_too_short_data():
    with pytest.raises(SWMFormatError, match="fixed header"):
        Instrument.decode(b"\x00" * 8, name=b"")


def test_instrument_decode_rejects_bad_pointers():
    # PW pointer must be > header size; here we set it to 0.
    data = bytearray(20)
    data[10] = 0  # PW table pointer in header
    data[11] = 0  # filter table pointer
    with pytest.raises(SWMFormatError, match="table pointers"):
        Instrument.decode(bytes(data), name=b"")


def test_instrument_rejects_out_of_range_byte_field():
    ins = Instrument(vibrato=999)
    with pytest.raises(SWMFormatError, match="vibrato"):
        ins.encode()


# --- Row ----------------------------------------------------------------


def test_row_nop_serialises_to_single_zero_byte():
    assert Row().encode() == b"\x00"


def test_row_with_note_only_is_one_byte():
    assert Row(note=0x31).encode() == bytes([0x31])


def test_row_with_note_and_instrument_sets_continuation_flag():
    assert Row(note=0x31, instrument=1).encode() == bytes([0x80 | 0x31, 0x01])


def test_row_with_instrument_only_uses_zero_note_byte_plus_flag():
    assert Row(instrument=2).encode() == bytes([0x80, 0x02])


def test_row_with_small_fx_only_emits_three_bytes():
    # No note, no instrument, just a small fx that needs no value.
    assert Row(fx=0x50).encode() == bytes([0x80, 0x80, 0x50])


def test_row_with_big_fx_requires_fx_value():
    with pytest.raises(SWMFormatError, match="big effect"):
        Row(fx=0x05).encode()


def test_row_with_big_fx_and_value_emits_four_bytes():
    assert Row(fx=0x05, fx_value=0x10).encode() == bytes([0x80, 0x80, 0x05, 0x10])


def test_row_rejects_fx_value_without_fx():
    with pytest.raises(SWMFormatError, match="fx_value"):
        Row(fx_value=0x10).encode()


def test_row_rejects_out_of_range_note():
    with pytest.raises(SWMFormatError, match="note"):
        Row(note=200).encode()


def test_row_rejects_out_of_range_instrument():
    with pytest.raises(SWMFormatError, match="instrument"):
        Row(instrument=200).encode()


def test_row_rejects_out_of_range_fx():
    with pytest.raises(SWMFormatError, match="fx"):
        Row(fx=0).encode()


# --- Pattern decode -----------------------------------------------------


def test_pattern_decode_handles_all_column_shapes():
    raw = (
        bytes([0x31])  # note only
        + bytes([0x80, 0x02])  # instrument only
        + bytes([0x80, 0x80, 0x50])  # small fx only
        + bytes([0x80, 0x80, 0x05, 0x10])  # big fx + value
        + b"\x00"  # NOP
        + bytes([0x80 | 0x31, 0x80 | 0x02, 0x05, 0x20])  # note+inst+big fx+value
    )
    ptn = Pattern.decode(raw, length=6)
    assert ptn.rows == [
        Row(note=0x31),
        Row(instrument=2),
        Row(fx=0x50),
        Row(fx=0x05, fx_value=0x10),
        Row(),
        Row(note=0x31, instrument=2, fx=0x05, fx_value=0x20),
    ]


def test_pattern_decode_rejects_truncated_row_at_inst_column():
    with pytest.raises(SWMFormatError, match="instrument column"):
        Pattern.decode(bytes([0x80]), length=1)


def test_pattern_decode_rejects_truncated_row_at_fx_column():
    with pytest.raises(SWMFormatError, match="fx column"):
        Pattern.decode(bytes([0x80, 0x80]), length=1)


def test_pattern_decode_rejects_truncated_big_fx_value():
    with pytest.raises(SWMFormatError, match="big effect but value missing"):
        Pattern.decode(bytes([0x80, 0x80, 0x05]), length=1)


def test_pattern_effective_length_uses_explicit_value_when_set():
    assert Pattern(rows=[Row()], length=32).effective_length() == 32
    assert Pattern(rows=[Row(), Row()]).effective_length() == 2


# --- Sequence commands --------------------------------------------------


def test_sequence_round_trip_play_pattern_transpose_tempo_end():
    cmds = [
        PlayPattern(1),
        Transpose(3),
        TempoOverride(6),
        PlayPattern(2),
        End(),
    ]
    raw = encode_sequence(cmds)
    assert decode_sequence(raw) == cmds


def test_transpose_round_trips_negative_semitones():
    raw = encode_sequence([Transpose(-16), Transpose(-1), Transpose(15), End()])
    assert decode_sequence(raw) == [
        Transpose(-16),
        Transpose(-1),
        Transpose(15),
        End(),
    ]


def test_loop_consumes_position_byte_on_round_trip():
    cmds = [PlayPattern(1), PlayPattern(2), Loop(position=1)]
    raw = encode_sequence(cmds)
    assert decode_sequence(raw) == cmds


def test_decode_sequence_a0_range_is_main_volume():
    raw = bytes([0x01, 0xA5, 0xFE])
    assert decode_sequence(raw) == [PlayPattern(1), MainVolume(5), End()]


def test_main_volume_round_trips_all_levels():
    cmds = [MainVolume(level) for level in range(16)]
    raw = encode_sequence(cmds)
    assert raw == bytes(0xA0 | level for level in range(16))
    assert decode_sequence(raw) == cmds


def test_main_volume_rejects_out_of_range_level():
    with pytest.raises(SWMFormatError, match="main volume"):
        MainVolume(16).encode()


def test_tempo_band_ends_at_0xef_and_f0_range_is_raw():
    # 0xB0..0xEF decode to TempoOverride; 0xF0..0xFD are player no-ops
    # preserved verbatim as RawSequenceByte (round-trip byte-exact).
    assert decode_sequence(bytes([0xB0])) == [TempoOverride(0)]
    assert decode_sequence(bytes([0xEF])) == [TempoOverride(0x3F)]
    for b in (0xF0, 0xF9, 0xFD):
        decoded = decode_sequence(bytes([b]))
        assert decoded == [RawSequenceByte(b)]
        assert encode_sequence(decoded) == bytes([b])


def test_tempo_override_encodes_top_of_band():
    assert TempoOverride(0x3F).encode() == bytes([0xEF])


def test_decode_sequence_preserves_trailing_ff_without_position():
    # A bare 0xFF at the end (no loop position byte) round-trips as a
    # RawSequenceByte rather than being silently dropped.
    raw = bytes([0x01, 0xFF])
    decoded = decode_sequence(raw)
    assert decoded == [PlayPattern(1), RawSequenceByte(0xFF)]
    assert encode_sequence(decoded) == raw


def test_play_pattern_rejects_out_of_range_value():
    with pytest.raises(SWMFormatError, match="pattern reference"):
        PlayPattern(128).encode()


def test_transpose_rejects_out_of_range_value():
    with pytest.raises(SWMFormatError, match="transpose"):
        Transpose(16).encode()


def test_tempo_override_rejects_out_of_range_value():
    with pytest.raises(SWMFormatError, match="tempo"):
        TempoOverride(78).encode()


def test_loop_rejects_out_of_range_position():
    with pytest.raises(SWMFormatError, match="loop position"):
        Loop(position=256).encode()


def test_raw_sequence_byte_rejects_out_of_range_value():
    with pytest.raises(SWMFormatError, match="raw sequence byte"):
        RawSequenceByte(256).encode()
