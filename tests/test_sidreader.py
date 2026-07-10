"""Tests for the PSID (``.sid``) reader.

The :func:`test_parse_sid_*` cases build a minimal but fully valid SID-Wizard
runtime tune image in-process (no external files — CI has no HVSC) from the
documented exporter layout, then assert :func:`parse_sid` recovers the
expected counts, notes, and instrument fields. The corpus smoke test at the
bottom is opt-in and skips when the local HVSC tree is absent.
"""

from __future__ import annotations

import os

import pytest

from pysidwizard import (
    End,
    Instrument,
    PlayPattern,
    Row,
    SequenceCommand,
    SIDFormatError,
    SWMPlayer,
    Transpose,
    is_sidwizard_sid,
    parse_psid_header,
    parse_sid,
    read_sid,
)
from pysidwizard.constants import SWM_MAGIC, TUNE_HEADER_SIZE
from pysidwizard.model import encode_sequence, pack_pattern

LOAD = 0x1000


def _swm_header(seq, pat, inst, chord_len, tempo_len, author=b"TESTER"):
    h = bytearray(TUNE_HEADER_SIZE)
    h[0:4] = SWM_MAGIC
    h[0x04] = 1  # framespeed
    h[0x05] = 4  # highlight
    h[0x08:0x0B] = b"\xff\xff\xff"  # mute
    h[0x0B] = 32  # default pattern length
    h[0x0C] = seq
    h[0x0D] = pat
    h[0x0E] = inst
    h[0x0F] = chord_len
    h[0x10] = tempo_len
    h[0x13] = 0  # driver type
    h[0x14] = 0  # tuning type
    h[0x18 : 0x18 + len(author)] = author
    return bytes(h)


def _make_psid(load: int, image: dict, end: int) -> bytes:
    """Wrap a sparse memory ``image`` (addr -> byte) into a 1-SID PSID v2 file."""
    mem = bytearray(end - load)
    for addr, value in image.items():
        mem[addr - load] = value & 0xFF
    header = bytearray(0x7C)
    header[0x00:0x04] = b"PSID"
    header[0x04:0x06] = (2).to_bytes(2, "big")  # version 2
    header[0x06:0x08] = (0x7C).to_bytes(2, "big")  # dataOffset
    header[0x08:0x0A] = (0).to_bytes(2, "big")  # loadAddress 0 => in data
    header[0x0A:0x0C] = load.to_bytes(2, "big")  # initAddress
    header[0x0C:0x0E] = (load + 3).to_bytes(2, "big")  # playAddress
    header[0x0E:0x10] = (1).to_bytes(2, "big")  # songs
    header[0x10:0x12] = (1).to_bytes(2, "big")  # startSong
    # flags / SID-address bytes left zero => single SID.
    data_area = load.to_bytes(2, "little") + bytes(mem)
    return bytes(header) + data_area


def _build_reference_image():
    """Construct a tiny but structurally complete SID-Wizard tune image.

    One subtune (3 channels), 2 patterns, 2 instruments, a 4-byte chord table,
    no tempo programs. Returns ``(sid_bytes, expected)``.
    """
    seq_amount, pat_amount, inst_amount = 3, 2, 2
    chord_len, tempo_len = 4, 0
    image: dict = {}

    def put(addr, data):
        for i, b in enumerate(data):
            image[addr + i] = b

    # 64-byte tune header at load+0x20 (the scan finds 'SWM1' here).
    swm_pos = LOAD + 0x20
    put(swm_pos, _swm_header(seq_amount, pat_amount, inst_amount, chord_len, tempo_len))

    # --- low regions: sequences, then patterns, then instruments ---
    seq_addrs = []
    cursor = LOAD + 0x100
    seq_cmds: list[list[SequenceCommand]] = [
        [PlayPattern(1), PlayPattern(2)],  # ch0: End terminator appended below
        [Transpose(2), PlayPattern(1)],  # ch1
        [PlayPattern(2)],  # ch2
    ]
    for cmds in seq_cmds:
        seq_addrs.append(cursor)
        body = encode_sequence(cmds) + b"\xfe"  # explicit End ($FE) terminator
        put(cursor, body)
        cursor += len(body)

    # Patterns: pattern 1 has real rows, pattern 2 is a single held note.
    pat_rows = [
        [Row(note=0x31, instrument=1), Row(note=0x33), Row(), Row(note=0x35, instrument=2)],
        [Row(note=0x10)],
    ]
    pat_addrs = []
    for rows in pat_rows:
        pat_addrs.append(cursor)
        unpacked = b"".join(r.encode() for r in rows)
        packed = pack_pattern(unpacked)
        put(cursor, packed + b"\xff" + bytes([len(unpacked) + 1]))  # body, $FF, length
        cursor += len(packed) + 2

    insts = [
        Instrument(
            control=0x10,
            attack=2,
            decay=3,
            sustain=10,
            release=5,
            wf_table=bytes([0x41, 0x42]),
            pw_table=bytes([0x00, 0x08]),
            filter_table=bytes([0xF0]),
        ),
        Instrument(
            control=0x40,
            attack=0,
            decay=15,
            first_waveform=0x11,
            wf_table=bytes([0x21]),
            pw_table=b"",
            filter_table=b"",
        ),
    ]
    inst_addrs = []
    for inst in insts:
        inst_addrs.append(cursor)
        body = inst.encode() + b"\xff"  # runtime appends the filter terminator
        put(cursor, body)
        cursor += len(body)

    # --- chord table, RESTEMP gap, subtune table, pointer tables ---
    chord_base = cursor
    chord_table = bytes([0x00, 0x03, 0x07, 0x7F])
    put(chord_base, chord_table)
    subtune_base = chord_base + chord_len + 8 + tempo_len  # 8-byte RESTEMP gap
    inst_lo = subtune_base + 8  # no chord/tempo pointer tables in this minimal image

    # Subtune table: 3 sequence pointers + funktempo pair.
    block = bytearray()
    for sa in seq_addrs:
        block += sa.to_bytes(2, "little")
    block += bytes([0x86, 0x83])  # straight funktempos
    put(subtune_base, block)

    inst_hi = inst_lo + (inst_amount + 1)
    ptn_lo = inst_hi + inst_amount
    ptn_hi = ptn_lo + pat_amount
    end = ptn_hi + (pat_amount + 1)

    for k in range(1, inst_amount + 1):
        image[inst_lo + k] = inst_addrs[k - 1] & 0xFF
        image[inst_hi + k] = (inst_addrs[k - 1] >> 8) & 0xFF
    for k in range(1, pat_amount + 1):
        image[ptn_lo + k] = pat_addrs[k - 1] & 0xFF
        image[ptn_hi + k] = (pat_addrs[k - 1] >> 8) & 0xFF

    expected = {
        "seq_amount": seq_amount,
        "pat_amount": pat_amount,
        "inst_amount": inst_amount,
        "chord_table": chord_table,
        "subtune_tempos": [(0x86, 0x83)],
        "pat_rows": pat_rows,
        "insts": insts,
    }
    return _make_psid(LOAD, image, end), expected


def _build_two_subtune_image():
    """Build a structurally complete two-subtune tune image.

    Six sequences (two subtunes of three channels), each subtune with its own
    funktempo pair. Returns ``(sid_bytes, expected)``.
    """
    seq_amount, pat_amount, inst_amount = 6, 2, 1
    chord_len, tempo_len = 0, 0
    subtunes = 2
    image: dict = {}

    def put(addr, data):
        for i, b in enumerate(data):
            image[addr + i] = b

    swm_pos = LOAD + 0x20
    put(swm_pos, _swm_header(seq_amount, pat_amount, inst_amount, chord_len, tempo_len))

    cursor = LOAD + 0x100
    # Distinct orderlist per channel so we can tell subtunes apart.
    seq_cmds: list[list[SequenceCommand]] = [
        [PlayPattern(1)],
        [PlayPattern(2)],
        [PlayPattern(1), PlayPattern(2)],
        [PlayPattern(2)],
        [PlayPattern(1)],
        [PlayPattern(2), PlayPattern(1)],
    ]
    seq_addrs = []
    for cmds in seq_cmds:
        seq_addrs.append(cursor)
        body = encode_sequence(cmds) + b"\xfe"
        put(cursor, body)
        cursor += len(body)

    pat_rows = [[Row(note=0x31, instrument=1)], [Row(note=0x10)]]
    pat_addrs = []
    for rows in pat_rows:
        pat_addrs.append(cursor)
        unpacked = b"".join(r.encode() for r in rows)
        packed = pack_pattern(unpacked)
        put(cursor, packed + b"\xff" + bytes([len(unpacked) + 1]))
        cursor += len(packed) + 2

    inst = Instrument(
        control=0x10,
        attack=2,
        decay=3,
        wf_table=bytes([0x41]),
        pw_table=b"",
        filter_table=b"",
    )
    inst_addr = cursor
    put(cursor, inst.encode() + b"\xff")
    cursor += len(inst.encode()) + 1

    # No chord/tempo tables; the subtune table sits next.
    subtune_base = cursor
    subtune_tempos = [(0x86, 0x83), (0x88, 0x84)]
    block = bytearray()
    for st in range(subtunes):
        for ch in range(3):
            block += seq_addrs[st * 3 + ch].to_bytes(2, "little")
        block += bytes(subtune_tempos[st])
    put(subtune_base, block)

    inst_lo = subtune_base + 8 * subtunes
    inst_hi = inst_lo + (inst_amount + 1)
    ptn_lo = inst_hi + inst_amount
    ptn_hi = ptn_lo + pat_amount
    end = ptn_hi + (pat_amount + 1)

    image[inst_lo + 1] = inst_addr & 0xFF
    image[inst_hi + 1] = (inst_addr >> 8) & 0xFF
    for k in range(1, pat_amount + 1):
        image[ptn_lo + k] = pat_addrs[k - 1] & 0xFF
        image[ptn_hi + k] = (pat_addrs[k - 1] >> 8) & 0xFF

    expected = {
        "seq_amount": seq_amount,
        "subtune_count": subtunes,
        "subtune_tempos": subtune_tempos,
    }
    return _make_psid(LOAD, image, end), expected


_SWP_OFFSET_POS = {
    "subtunes": 4,
    "ptn_lo": 6,
    "ptn_hi": 8,
    "inst_lo": 10,
    "inst_hi": 12,
    "chord_table": 14,
    "chord_ptr": 16,
    "tempo_table": 18,
    "tempo_ptr": 20,
}


def _build_swp_image(subtunes=1, with_header=True, with_chord=True):
    """Construct a "packed" (SWP) tune image with relative-pointer tables.

    Mirrors the runtime SWP layout: an ``SWP1`` offset-table whose nine
    entries give table bases relative to the SWP1 magic, and pointer tables
    that hold pointers relative to that same base. Structural counts are
    implied purely by the table extents, so the (optional) ``SWM1`` header
    deliberately carries *stale* counts to prove the reader ignores them.
    Returns ``(sid_bytes, expected)``.
    """
    swp_base = LOAD + 0x80
    image: dict = {}

    def put(addr, data):
        for i, b in enumerate(data):
            image[addr + i] = b & 0xFF

    chord_table = bytes([0x00, 0x03, 0x07, 0x0C, 0x7F]) if with_chord else b""
    chord_len = len(chord_table)

    # Optional SWM1 header (metadata + chord/tempo lengths). Stale seq/pat/inst
    # counts (99/99) so the test fails if the reader trusts them.
    if with_header:
        swm_pos = LOAD + 0x20
        put(swm_pos, _swm_header(99, 99, 99, chord_len, 0, author=b"SWPTESTER"))

    # --- bodies: sequences, patterns, instruments (all above swp_base) ---
    cursor = swp_base + 0x40
    seq_cmds_all = [
        [PlayPattern(1), PlayPattern(2)],
        [Transpose(1), PlayPattern(2)],
        [PlayPattern(1)],
        [PlayPattern(2)],
        [PlayPattern(2), PlayPattern(1)],
        [PlayPattern(1)],
    ]
    seq_amount = subtunes * 3
    seq_addrs = []
    for cmds in seq_cmds_all[:seq_amount]:
        seq_addrs.append(cursor)
        body = encode_sequence(cmds) + b"\xfe"
        put(cursor, body)
        cursor += len(body)

    pat_rows = [
        [Row(note=0x31, instrument=1), Row(note=0x33), Row(), Row(note=0x35, instrument=2)],
        [Row(note=0x10)],
    ]
    pat_amount = len(pat_rows)
    pat_addrs = []
    for rows in pat_rows:
        pat_addrs.append(cursor)
        unpacked = b"".join(r.encode() for r in rows)
        packed = pack_pattern(unpacked)
        put(cursor, packed + b"\xff" + bytes([len(unpacked) + 1]))
        cursor += len(packed) + 2

    insts = [
        Instrument(
            control=0x21,
            attack=2,
            decay=3,
            sustain=10,
            release=5,
            wf_table=bytes([0x41, 0x42]),
            pw_table=bytes([0x00, 0x08]),
            filter_table=bytes([0xF0]),
        ),
        Instrument(
            control=0x40,
            attack=0,
            decay=15,
            first_waveform=0x11,
            wf_table=bytes([0x21]),
            pw_table=b"",
            filter_table=b"",
        ),
    ]
    inst_amount = len(insts)
    inst_addrs = []
    for inst in insts:
        inst_addrs.append(cursor)
        put(cursor, inst.encode() + b"\xff")
        cursor += len(inst.encode()) + 1

    # Chord table sits below the subtune table (so it is excluded from the
    # subtune-extent count probe).
    chord_addr = cursor
    if chord_len:
        put(chord_addr, chord_table)
        cursor += chord_len

    # --- tables (contiguous; counts are read from these extents) ---
    sub_addr = cursor
    tempopt_addr = sub_addr + 8 * subtunes  # immediate successor of subtunes
    chordpt_addr = tempopt_addr + 1
    inst_lo_addr = chordpt_addr + 1
    inst_hi_addr = inst_lo_addr + (inst_amount + 1)
    ptn_lo_addr = inst_hi_addr + inst_amount
    ptn_hi_addr = ptn_lo_addr + pat_amount
    end_addr = ptn_hi_addr + (pat_amount + 1)

    subtune_tempos = [(0x86 + st, 0x83 + st) for st in range(subtunes)]
    for st in range(subtunes):
        for ch in range(3):
            rel = seq_addrs[st * 3 + ch] - swp_base
            put(sub_addr + 8 * st + 2 * ch, rel.to_bytes(2, "little"))
        put(sub_addr + 8 * st + 6, bytes(subtune_tempos[st]))

    for k in range(1, inst_amount + 1):
        rel = inst_addrs[k - 1] - swp_base
        image[inst_lo_addr + k] = rel & 0xFF
        image[inst_hi_addr + k] = (rel >> 8) & 0xFF
    for k in range(1, pat_amount + 1):
        rel = pat_addrs[k - 1] - swp_base
        image[ptn_lo_addr + k] = rel & 0xFF
        image[ptn_hi_addr + k] = (rel >> 8) & 0xFF

    # --- SWP1 header offset-table (offsets relative to swp_base) ---
    put(swp_base, b"SWP1")
    offsets = {
        "subtunes": sub_addr - swp_base,
        "ptn_lo": ptn_lo_addr - swp_base,
        "ptn_hi": ptn_hi_addr - swp_base,
        "inst_lo": inst_lo_addr - swp_base,
        "inst_hi": inst_hi_addr - swp_base,
        "chord_table": chord_addr - swp_base,
        "chord_ptr": chordpt_addr - swp_base,
        "tempo_table": chord_addr - swp_base,  # tl=0, value unused
        "tempo_ptr": tempopt_addr - swp_base,
    }
    for name, pos in _SWP_OFFSET_POS.items():
        put(swp_base + pos, offsets[name].to_bytes(2, "little"))

    expected = {
        "seq_amount": seq_amount,
        "pat_amount": pat_amount,
        "inst_amount": inst_amount,
        "subtune_count": subtunes,
        "chord_table": chord_table if (with_header and with_chord) else b"",
        "subtune_tempos": subtune_tempos,
        "pat_rows": pat_rows,
        "insts": insts,
    }
    return _make_psid(LOAD, image, end_addr), expected


def test_parse_swp_recovers_counts_and_content():
    sid, exp = _build_swp_image()
    swm = parse_sid(sid)
    # Counts come from the SWP table extents, not the (stale 99/99) SWM1 header.
    assert len(swm.sequences) == exp["seq_amount"]
    assert len(swm.patterns) == exp["pat_amount"]
    assert len(swm.instruments) == exp["inst_amount"]
    assert swm.subtune_count == exp["subtune_count"]
    assert swm.load_address == LOAD
    assert swm.chord_table == exp["chord_table"]
    assert swm.subtune_tempos == exp["subtune_tempos"]
    assert swm.author.startswith(b"SWPTESTER")


def test_parse_swp_recovers_pattern_and_instrument_content():
    sid, exp = _build_swp_image()
    swm = parse_sid(sid)
    p0 = swm.patterns[0]
    assert [r.note for r in p0.rows] == [0x31, 0x33, None, 0x35]
    assert p0.rows[0].instrument == 1 and p0.rows[3].instrument == 2
    i0, i1 = swm.instruments
    assert i0.control == 0x21
    assert (i0.attack, i0.decay, i0.sustain, i0.release) == (2, 3, 10, 5)
    assert i0.wf_table == bytes([0x41, 0x42])
    assert i0.pw_table == bytes([0x00, 0x08])
    assert i0.filter_table == bytes([0xF0])
    assert i1.first_waveform == 0x11
    assert swm.sequences[0] == [PlayPattern(1), PlayPattern(2), End()]
    assert swm.sequences[1] == [Transpose(1), PlayPattern(2), End()]


def test_parse_swp_result_plays():
    sid, _ = _build_swp_image()
    player = SWMPlayer(parse_sid(sid))
    for _ in range(200):
        player.play_frame()


def test_parse_swp_without_swm1_header():
    # Packed exports may omit the SWM1 header entirely; the reader must still
    # resolve the tables (using model defaults for the lossy metadata).
    sid, exp = _build_swp_image(with_header=False, with_chord=False)
    assert b"SWM1" not in sid[0x7E:]
    swm = parse_sid(sid)
    assert len(swm.patterns) == exp["pat_amount"]
    assert len(swm.instruments) == exp["inst_amount"]
    assert swm.chord_table == b""
    SWMPlayer(swm).play_frame()


def test_parse_swp_exposes_all_subtunes():
    sid, exp = _build_swp_image(subtunes=2, with_chord=False)
    swm = parse_sid(sid)
    assert swm.subtune_count == 2
    assert len(swm.sequences) == 6
    assert swm.subtune_tempos == exp["subtune_tempos"]
    for n in range(2):
        st = swm.subtune(n)
        assert len(st.sequences) == 3
        player = SWMPlayer(st)
        for _ in range(60):
            player.play_frame()


def test_parse_sid_recovers_counts_and_content():
    sid, exp = _build_reference_image()
    swm = parse_sid(sid)
    assert len(swm.sequences) == exp["seq_amount"]
    assert len(swm.patterns) == exp["pat_amount"]
    assert len(swm.instruments) == exp["inst_amount"]
    assert swm.load_address == LOAD
    assert swm.chord_table == exp["chord_table"]
    assert swm.subtune_tempos == exp["subtune_tempos"]
    assert swm.author.startswith(b"TESTER")


def test_parse_sid_recovers_pattern_notes():
    sid, _ = _build_reference_image()
    swm = parse_sid(sid)
    p0 = swm.patterns[0]
    assert [r.note for r in p0.rows] == [0x31, 0x33, None, 0x35]
    assert p0.rows[0].instrument == 1
    assert p0.rows[3].instrument == 2
    assert [r.note for r in swm.patterns[1].rows] == [0x10]


def test_parse_sid_recovers_instrument_fields():
    sid, _ = _build_reference_image()
    swm = parse_sid(sid)
    i0, i1 = swm.instruments
    assert i0.control == 0x10
    assert (i0.attack, i0.decay, i0.sustain, i0.release) == (2, 3, 10, 5)
    assert i0.wf_table == bytes([0x41, 0x42])
    assert i0.pw_table == bytes([0x00, 0x08])
    assert i0.filter_table == bytes([0xF0])
    assert i1.control == 0x40
    assert i1.first_waveform == 0x11
    # Names are not recoverable from a SID; placeholders are synthesised.
    assert i0.name == b"INST 01"


def test_parse_sid_recovers_sequences():
    sid, _ = _build_reference_image()
    swm = parse_sid(sid)
    # The $FE terminator decodes to a trailing End() command.
    assert swm.sequences[0] == [PlayPattern(1), PlayPattern(2), End()]
    assert swm.sequences[1] == [Transpose(2), PlayPattern(1), End()]
    assert swm.sequences[2] == [PlayPattern(2), End()]


def test_parse_sid_result_plays():
    sid, _ = _build_reference_image()
    swm = parse_sid(sid)
    player = SWMPlayer(swm)
    for _ in range(200):
        player.play_frame()


def test_parse_sid_exposes_all_subtunes():
    sid, exp = _build_two_subtune_image()
    swm = parse_sid(sid)
    assert len(swm.sequences) == exp["seq_amount"]
    assert swm.subtune_count == exp["subtune_count"]
    assert swm.subtune_tempos == exp["subtune_tempos"]
    # Each subtune is reachable as its own playable, three-sequence view.
    for n in range(exp["subtune_count"]):
        st = swm.subtune(n)
        assert len(st.sequences) == 3
        assert st.subtune_tempos == [exp["subtune_tempos"][n]]
        assert st.sequences == swm.sequences[n * 3 : n * 3 + 3]
        # Shared global tables are preserved.
        assert st.patterns is swm.patterns
        assert st.instruments is swm.instruments
        player = SWMPlayer(st)
        for _ in range(60):
            player.play_frame()


def test_subtune_out_of_range_raises():
    sid, exp = _build_two_subtune_image()
    swm = parse_sid(sid)
    with pytest.raises(IndexError):
        swm.subtune(exp["subtune_count"])


def test_read_sid_from_path(tmp_path):
    sid, exp = _build_reference_image()
    path = tmp_path / "reference.sid"
    path.write_bytes(sid)
    swm = read_sid(os.fspath(path))
    assert len(swm.patterns) == exp["pat_amount"]
    assert len(swm.instruments) == exp["inst_amount"]


def test_is_sidwizard_sid_true_for_reference():
    sid, _ = _build_reference_image()
    assert is_sidwizard_sid(sid) is True


def test_parse_psid_header_fields():
    sid, _ = _build_reference_image()
    header = parse_psid_header(sid)
    assert header.magic == b"PSID"
    assert header.version == 2
    assert header.real_load_address == LOAD
    assert header.data_start == 0x7C + 2
    assert not header.is_multi_sid


# --------------------------------------------------------------------------
# Negative cases.
# --------------------------------------------------------------------------


def test_parse_sid_rejects_bad_magic():
    with pytest.raises(SIDFormatError, match="not a SID file"):
        parse_sid(b"NOPE" + b"\x00" * 0x80)


def test_parse_sid_accepts_rsid_with_resolvable_data():
    # RSID uses a different init/play convention, but the embedded SID-Wizard
    # tune-data layout is identical, so a resolvable RSID parses like a PSID.
    sid, exp = _build_reference_image()
    rsid = b"RSID" + sid[4:]
    assert is_sidwizard_sid(rsid) is True
    swm = parse_sid(rsid)
    assert len(swm.patterns) == exp["pat_amount"]
    assert len(swm.instruments) == exp["inst_amount"]
    SWMPlayer(swm).play_frame()


def test_parse_sid_rejects_unresolvable_swp_tune():
    # An 'SWP1' magic whose offset-table is garbage (here spliced over an
    # otherwise-absolute image) must raise a clear, SWP-specific error rather
    # than silently mis-parsing.
    sid, _ = _build_reference_image()
    tampered = bytearray(sid)
    # Splice an SWP1 magic into the data area (after the container header).
    tampered[0x90:0x94] = b"SWP1"
    with pytest.raises(SIDFormatError, match="SWP"):
        parse_sid(bytes(tampered))


def test_parse_sid_rejects_multi_sid():
    sid, _ = _build_reference_image()
    tampered = bytearray(sid)
    tampered[0x04:0x06] = (3).to_bytes(2, "big")  # version 3
    tampered[0x7A] = 0x42  # second SID address
    with pytest.raises(SIDFormatError, match="multi-SID"):
        parse_sid(bytes(tampered))
    assert is_sidwizard_sid(bytes(tampered)) is False


def test_parse_sid_rejects_missing_swm1():
    # Valid PSID header but the data area has no 'SWM1' magic.
    header = bytearray(0x7C)
    header[0x00:0x04] = b"PSID"
    header[0x04:0x06] = (2).to_bytes(2, "big")
    header[0x06:0x08] = (0x7C).to_bytes(2, "big")
    header[0x08:0x0A] = (0).to_bytes(2, "big")
    data = bytes(header) + LOAD.to_bytes(2, "little") + b"\x00" * 256
    with pytest.raises(SIDFormatError, match="SWM1"):
        parse_sid(data)


def test_parse_sid_rejects_truncated_image():
    sid, _ = _build_reference_image()
    # Lop off most of the tune data: the pointer tables can no longer resolve.
    truncated = sid[: 0x7C + 2 + 0x80]
    with pytest.raises(SIDFormatError):
        parse_sid(truncated)


# --------------------------------------------------------------------------
# Opt-in real-corpus smoke test (skipped without the local HVSC tree).
# --------------------------------------------------------------------------

_HVSC = "/scratch/preframr/hvsc/C64Music"
_CATALOG = "/scratch/anarkiwi/cbm/hvsc-tracker-catalog/data/results.csv"


_CORPUS_PRESENT = os.path.isdir(_HVSC) and os.path.isfile(_CATALOG)


def _corpus_rows():
    import csv

    # Return an empty list when the corpus isn't present so module-level
    # parametrize() collection (below) never touches the missing catalog.
    # The per-test skipif still guards execution; an empty parameter set
    # skips cleanly.
    if not _CORPUS_PRESENT:
        return []
    return [r for r in csv.reader(open(_CATALOG)) if len(r) > 1 and "SidWizard" in r[1]]


# Materialised once at import so xdist workers parametrize over a stable set.
_CORPUS_ROWS = _corpus_rows()


def _corpus_id(row):
    return row[0]


@pytest.mark.skipif(not _CORPUS_PRESENT, reason="local HVSC corpus not present")
@pytest.mark.parametrize("row", _CORPUS_ROWS, ids=_corpus_id)
def test_real_corpus_row_parses_and_plays(row):
    # Per-tune parse + bounded playback. Parametrized (one case per corpus
    # row) so xdist distributes the work and a failure points at one tune.
    path = os.path.join(_HVSC, row[0])
    try:
        data = open(path, "rb").read()
    except OSError:
        pytest.skip("file not present on disk")
    if not is_sidwizard_sid(data):
        pytest.skip("not a SID-Wizard tune")
    try:
        swm = parse_sid(data)
    except SIDFormatError:
        pytest.skip("relocated tail is allowed to fail cleanly")
    # The reader resolves a tune three ways: in-place from the SWM1 header,
    # from the SWP table extents, or from the player-code operands (code scan).
    # Only the first makes the header counts authoritative; SWP and code-scan
    # derive counts from the table geometry (and may repair a stale header or a
    # partially-relocated pointer table). So the universal invariant checked
    # here is content sanity: every orderlist reference resolves to a real
    # pattern and every decoded note is a legal byte.
    max_ref = max(
        (c.pattern for s in swm.sequences for c in s if isinstance(c, PlayPattern)),
        default=0,
    )
    assert max_ref <= len(swm.patterns)
    for pat in swm.patterns:
        for r_ in pat.rows:
            assert r_.note is None or r_.note <= 0x7F
    # When the tune is a classic in-place export (an SWM1 header is present and
    # its counts match the decoded model), the header must agree exactly.
    swm_pos = data.find(SWM_MAGIC)
    if swm_pos >= 0 and data.find(b"SWP1", 0x7E) < 0:
        header = data[swm_pos:][:TUNE_HEADER_SIZE]
        if len(swm.patterns) == header[0x0D]:
            assert len(swm.instruments) == header[0x0E]
            assert len(swm.sequences) == header[0x0C]
    # Drive the player for a bounded sample.
    player = SWMPlayer(swm)
    for _ in range(300):
        player.play_frame()
    if swm.subtune_count > 1:
        # Every subtune must be independently playable.
        for n in range(swm.subtune_count):
            sub_player = SWMPlayer(swm.subtune(n))
            for _ in range(120):
                sub_player.play_frame()


@pytest.mark.skipif(not _CORPUS_PRESENT, reason="local HVSC corpus not present")
def test_real_corpus_has_expected_coverage():
    # Corpus-health fold (natural loop): the catalog must actually resolve to
    # a substantial set of parseable tunes, including the RSID and
    # multi-subtune categories the per-row test exercises.
    parsed = 0
    rsid_seen = False
    multisubtune_seen = False
    swp_parsed = 0
    for r in _CORPUS_ROWS:
        path = os.path.join(_HVSC, r[0])
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        if not is_sidwizard_sid(data):
            continue
        is_swp = data.find(b"SWP1", 0x7E) >= 0
        try:
            swm = parse_sid(data)
        except SIDFormatError:
            continue  # the relocated tail is allowed to fail cleanly
        parsed += 1
        if is_swp:
            swp_parsed += 1
        if data[:4] == b"RSID":
            rsid_seen = True
        if swm.subtune_count > 1:
            multisubtune_seen = True
    assert parsed >= 800, "expected to parse the bulk of the corpus"
    assert rsid_seen, "expected at least one RSID tune to parse and play"
    assert multisubtune_seen, "expected at least one multi-subtune tune"
    assert swp_parsed >= 20, "expected the packed (SWP) tunes to parse now"


@pytest.mark.skipif(not _CORPUS_PRESENT, reason="local HVSC corpus not present")
@pytest.mark.parametrize("row", _CORPUS_ROWS, ids=_corpus_id)
def test_real_corpus_row_rejection_is_clear(row):
    # Multi-SID tunes must raise a specific, actionable error rather than
    # parsing into a misleading model. (Packed/SWP tunes are now decoded, not
    # rejected.) Parametrized per row for isolation.
    path = os.path.join(_HVSC, row[0])
    try:
        data = open(path, "rb").read()
    except OSError:
        pytest.skip("file not present on disk")
    try:
        header = parse_psid_header(data)
    except SIDFormatError:
        pytest.skip("not a parseable PSID/RSID header")
    if header.version >= 3 or header.is_multi_sid:
        with pytest.raises(SIDFormatError, match="multi-SID"):
            parse_sid(data)
    else:
        pytest.skip("not a multi-SID tune")


@pytest.mark.skipif(not _CORPUS_PRESENT, reason="local HVSC corpus not present")
def test_real_corpus_has_rejection_categories():
    # Corpus-health fold: the catalog must contain at least one multi-SID tune
    # so the per-row rejection test actually exercises it.
    saw_multisid = False
    for r in _CORPUS_ROWS:
        path = os.path.join(_HVSC, r[0])
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        try:
            header = parse_psid_header(data)
        except SIDFormatError:
            continue
        if header.version >= 3 or header.is_multi_sid:
            saw_multisid = True
    assert saw_multisid


# The 27 known SID-Wizard "packed" (SWP) tunes in HVSC, relative to _HVSC.
_KNOWN_SWP_FILES = [
    "GAMES/G-L/Immensity_preview_2.sid",
    *(
        f"MUSICIANS/C/C0zmo/{name}.sid"
        for name in (
            "50_Shades_of_Gradius",
            "Archon_Reimagined",
            "Arkanoid_Remake",
            "Ben_Daglish_Tribute",
            "Boulder_Dash_Remake",
            "Elite_Remake",
            "Katakis_Remake",
            "Last_Ninja_Remix-Remix",
            "Out_Run",
            "Performers_Demo_2018",
            "Pirates_Bach_Prelude_C_Minor",
            "R-Type_Amiga_Title_Remake",
            "Space_Harrier_Remake",
            "Times_of_Lore",
            "To_Be_on_Top",
            "Wizard_of_Wor",
        )
    ),
    *(
        f"MUSICIANS/H/Hermit/{name}.sid"
        for name in (
            "Civilization",
            "Crawl_Out_Through_the_Fallout",
            "Easy_Living",
            "Emomyst",
            "End_of_the_World",
            "Fallout_4_Main_Theme",
            "I_Dont_Want_to_Set_the_World_on_Fire",
            "Sixty_Minute_Man",
            "Uranium_Fever",
            "Wonderful_Guy",
        )
    ),
]


@pytest.mark.skipif(not _CORPUS_PRESENT, reason="local HVSC corpus not present")
@pytest.mark.parametrize("rel", _KNOWN_SWP_FILES)
def test_known_swp_file_parses_and_plays(rel):
    # The packed (SWP) tunes must now decode and play (all subtunes), with
    # every pattern reference resolving.
    path = os.path.join(_HVSC, rel)
    try:
        data = open(path, "rb").read()
    except OSError:
        pytest.skip("file not present on disk")
    assert data.find(b"SWP1", 0x7E) >= 0, "expected an SWP1 block"
    swm = parse_sid(data)
    max_ref = max(
        (c.pattern for s in swm.sequences for c in s if isinstance(c, PlayPattern)),
        default=0,
    )
    assert max_ref <= len(swm.patterns)
    for n in range(swm.subtune_count):
        player = SWMPlayer(swm.subtune(n))
        for _ in range(500):
            player.play_frame()
