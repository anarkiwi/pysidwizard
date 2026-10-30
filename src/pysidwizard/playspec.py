"""The SID-Wizard playroutine as a ``pysidtracker.playspec`` spec.

``specs/sidwizard.play`` states the pattern row reader; :func:`binding` resolves its
``layout`` from the player's own code, which is the same in every build.
"""

from importlib import resources

from pysidtracker import CodePattern, SidImage, find_code_all
from pysidtracker.playspec import parse as parse_spec

SPEC = parse_spec(resources.files(__package__).joinpath("specs/sidwizard.play").read_text())

# The voice's tick clock: ``ldy TMPPOS,x ; lda SPDCNT,x ; iny ; sec ; sbc TEMPOTBL,y ; beq ; bvc ;
# ldy TMPSTART,x ; lda #0 ; sta SPDCNT,x ; tya ; sta TMPPOS,x ; lda SPDCNT,x ; inc SPDCNT,x ;
# cmp #0 ; beq <row>``, then ``jmp <other tick>``.
CLOCK = CodePattern(
    "BC {tmppos:w} BD {spdcnt:w} C8 38 F9 {tempotbl:w} F0 07 50 0C BC {tmpstart:w} A9 00 "
    "9D {spdcnt:w} 98 9D {tmppos:w} BD {spdcnt:w} FE {spdcnt:w} C9 00 F0 03"
)
# The same clock in a build without tempo programs: one tempo byte, no TMPPOS.
CLOCK_SIMPLE = CodePattern(
    "BD {spdcnt:w} 38 ED {tempo:w} F0 04 50 05 A9 00 9D {spdcnt:w} BD {spdcnt:w} FE {spdcnt:w} "
    "C9 00 F0 03"
)
# ``ldy PTNPOS,x ; lda NOPCNT,x ; bne ; lda (zp),y ; cmp #$70 ; bcc ; cmp #$78 ; bcs ;
# sbc #$6d ; sta NOPCNT,x ; dec NOPCNT,x ; lda #0 ; cmp #0 ; bmi`, then the note, instrument,
# effect and value columns, then ``tya ; sta PTNPOS,x``.
ROW_READER = CodePattern(
    "BC {ptnpos:w} BD {nopcnt:w} D0 ?? B1 {zp} C9 70 90 ?? C9 78 B0 ?? E9 6D 9D {nopcnt:w} "
    "DE {nopcnt:w} A9 00 C9 00 30 ?? 9D {curnote:w} 10 ?? 29 7F 9D {curnote:w} C8 B1 {zp} "
    "30 ?? 9D {curifx:w} 10 ?? 29 7F 9D {curifx:w} C8 B1 {zp} 9D {curfx:w} 29 E0 D0 ?? "
    "C8 B1 {zp} 9D {fxval:w} 98 9D {ptnpos:w}"
)


def image(view) -> SidImage:
    """The 64 KiB ``view`` as a :class:`~pysidtracker.SidImage`."""
    return SidImage.from_prg(b"\0\0" + bytes(view))


def _row_reader(view):
    for match in find_code_all(image(view), ROW_READER):
        return match
    raise ValueError("no SID-Wizard row reader in the image")


def _clock(view):
    """The tick clock of the player instance whose row reader is found: the nearest one.

    A build without tempo programs has the simpler clock; ``tempoprg`` says which.
    """
    reader = _row_reader(view).addr
    found = [(m, True) for m in find_code_all(image(view), CLOCK)]
    found += [(m, False) for m in find_code_all(image(view), CLOCK_SIMPLE)]
    if not found:
        raise ValueError("no SID-Wizard tick clock in the image")
    return min(found, key=lambda hit: abs(hit[0].addr - reader))


def row_reader(view) -> int:
    """The address of the row reader: its ``LDY PTNPOS,X``."""
    return _row_reader(view).addr


def reader_end(view) -> int:
    """The address after the row reader's last instruction (``STA PTNPOS,X``)."""
    return _row_reader(view).addr + ROW_READER.length


def clock(view) -> int:
    """The address of the tick clock: its first instruction."""
    return _clock(view)[0].addr


def clock_stop(view) -> int:
    """The clock's ``CMP #0``: where both its exits have written all they write."""
    match, programs = _clock(view)
    return match.addr + (CLOCK if programs else CLOCK_SIMPLE).length - 4


def binding(view) -> dict:
    """The playspec layout of the SID-Wizard player in the 64 KiB ``view``."""
    found = dict(_row_reader(view).captures)
    found["zp_ptn"] = found.pop("zp")
    match, programs = _clock(view)
    found.update({"tmppos": 0, "tmpstart": 0, "tempotbl": 0, "tempo": 0})
    found.update(match.captures)
    found["tempoprg"] = programs
    return found
