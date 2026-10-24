"""SID-Wizard player build (driver-type) feature flags.

The SWM header driver byte is SID-Wizard's ``PLAYERTYPE`` (``player.asm:183``
emits it; ``SWMconvert.c:58-59`` names 0..5). The six builds are one
``player.asm`` assembled with the ``feature`` blocks below. A ``.sid`` export's
player code is the authority: a packed (SWP) game player is assembled from an
edited ``settings.cfg``, so :func:`detect_features` reads each flag the player
models from its ``.if feature.X_ON`` site in the code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace
from typing import Dict, Optional

# Driver byte values (SWMconvert.c:58-59 / altplayers.inc PLAYERTYPE lines).
DRIVER_NORMAL = 0
DRIVER_MEDIUM = 1
DRIVER_LIGHT = 2
DRIVER_EXTRA = 3
DRIVER_BARE = 4
DRIVER_DEMO = 5


@dataclass(frozen=True)
class PlayerFeatures:
    """One build's compile-time ``feature`` block.

    Field names are the assembly flag names lower-cased without ``_ON``, so
    each maps 1:1 onto a ``.if feature.X_ON`` site in ``player.asm``. Flags
    with the same value in every build are omitted (they gate nothing).
    """

    driver_type: int
    name: str

    transposesupp: bool  # SEQ-FX transpose (player.asm:1650)
    octaveshift: bool  # per-instrument octave shift
    chordsupport: bool  # chord table callable from the arp table
    calcvibrato: bool  # calculated (vs table) vibrato
    vibratotypes: bool  # selectable vibrato types (player.asm:2087)
    detunesupport: bool  # detune column audible
    portavibra: bool  # drop back to vibrato at portamento target (2096/2118)
    arpspeedsupp: bool  # arp speed honoured (multispeed)
    portame_notefx: bool  # portamento note-FX $FF (player.asm:1529)
    gateoffptr: bool  # gate-off WF/PW/filter table pointer
    filtresetsw: bool  # filter-table reset on note-without-instrument
    filtkbtrack: bool  # filter cutoff keyboard tracking
    pwresetsw: bool  # PW-table reset switch
    pwkeybtrack: bool  # PW keyboard tracking (player.asm:1914-1924)
    wfarp_nop_supp: bool  # $80 NOP row in the WF arp table
    finefiltsweep: bool  # 11-bit (vs 8-bit) filter sweep
    hardrestypes: bool  # per-instrument HR ADSR + timer
    frame1switch: bool  # per-instrument 1-frame HR switch
    tempoprgsupp: bool  # tempo programs + funktempo pattern-FX
    fastspeedbind: bool  # collapse TICK_0..2 at tempo 1-2 (1496-1514, 1704-1723)
    delaysupport: bool  # track/note delay $1D/$1E (3730-3753)
    subtunejump: bool  # subtune jumping from the orderlist
    subtunesupport: bool  # subtunes at all
    multispeedsupp: bool  # multispeed (MULPLY) ticks
    allghostregs: bool  # ghost registers for ADSR/PW too (300, 443, 3278-3315)
    retainzeropage: bool  # save/restore zeropage around the player
    filteralways: bool  # HRENDER exit target (player.asm:1614-1622)
    pulsealways: bool  # HRENDER exit target (player.asm:1614-1622)
    vibslidealways: bool  # HRENDER exit target (player.asm:1614-1622)
    filt_ctrl_fx: bool  # $1F / $Bx filter-switch + resonance FX
    filtshift_supp: bool  # $1C filter shift
    seq_fx_support: bool  # orderlist FX
    volset_support: bool  # main-volume FX
    vibfreqfx_supp: bool  # $Dx / $16 vibrato-frequency FX
    filter_smallfx: bool  # $Bx / $Fx small-FX
    detune_smallfx: bool  # $8x / $9x small-FX
    wfctrl_smallfx: bool  # $4x / $Ex small-FX

    def flag_names(self) -> Dict[str, bool]:
        """The flag fields as an ``{ASSEMBLY_NAME_ON: value}`` mapping."""
        return {
            f.name.upper() + "_ON": getattr(self, f.name) for f in fields(self) if f.type == "bool"
        }


# settings.cfg:223-259 (normal build); its SUBTUNESUPPORT_ON is a compile-time expression only bare/demo hardwire to 0.
_NORMAL_FLAGS: Dict[str, bool] = {
    "transposesupp": True,
    "octaveshift": True,
    "chordsupport": True,
    "calcvibrato": True,
    "vibratotypes": True,
    "detunesupport": True,
    "portavibra": False,
    "arpspeedsupp": True,
    "portame_notefx": True,
    "gateoffptr": True,
    "filtresetsw": True,
    "filtkbtrack": True,
    "pwresetsw": True,
    "pwkeybtrack": True,
    "wfarp_nop_supp": True,
    "finefiltsweep": True,
    "hardrestypes": True,
    "frame1switch": True,
    "tempoprgsupp": True,
    "fastspeedbind": False,
    "delaysupport": False,
    "subtunejump": True,
    "subtunesupport": True,
    "multispeedsupp": True,
    "allghostregs": False,
    "retainzeropage": True,
    "filteralways": False,
    "pulsealways": False,
    "vibslidealways": False,
    "filt_ctrl_fx": True,
    "filtshift_supp": True,
    "seq_fx_support": True,
    "volset_support": True,
    "vibfreqfx_supp": True,
    "filter_smallfx": True,
    "detune_smallfx": True,
    "wfctrl_smallfx": True,
}


def _build(driver_type: int, name: str, **overrides: bool) -> PlayerFeatures:
    """A build's flags = the normal build's flags plus the listed overrides."""
    return PlayerFeatures(driver_type=driver_type, name=name, **{**_NORMAL_FLAGS, **overrides})


BUILD_FLAGS: Dict[int, PlayerFeatures] = {
    DRIVER_NORMAL: _build(DRIVER_NORMAL, "normal"),
    DRIVER_MEDIUM: _build(  # altplayers.inc:333-372
        DRIVER_MEDIUM,
        "medium",
        vibratotypes=False,
        gateoffptr=False,
        pwkeybtrack=False,
        hardrestypes=False,
        frame1switch=False,
        subtunejump=False,
        retainzeropage=False,
        filt_ctrl_fx=False,
    ),
    DRIVER_LIGHT: _build(  # altplayers.inc:434-473
        DRIVER_LIGHT,
        "light",
        transposesupp=False,
        chordsupport=False,
        calcvibrato=False,
        vibratotypes=False,
        detunesupport=False,
        arpspeedsupp=False,
        octaveshift=False,
        gateoffptr=False,
        filtresetsw=False,
        filtkbtrack=False,
        pwresetsw=False,
        pwkeybtrack=False,
        hardrestypes=False,
        frame1switch=False,
        tempoprgsupp=False,
        subtunejump=False,
        retainzeropage=False,
        finefiltsweep=False,
        filt_ctrl_fx=False,
    ),
    DRIVER_EXTRA: _build(  # altplayers.inc:535-574
        DRIVER_EXTRA,
        "extra",
        portavibra=True,
        fastspeedbind=True,
        delaysupport=True,
        allghostregs=True,
        filteralways=True,
        pulsealways=True,
        vibslidealways=True,
    ),
    DRIVER_BARE: PlayerFeatures(  # altplayers.inc:636-675: everything off
        driver_type=DRIVER_BARE,
        name="bare",
        **dict.fromkeys(_NORMAL_FLAGS, False),
    ),
    DRIVER_DEMO: _build(  # altplayers.inc:737-776
        DRIVER_DEMO,
        "demo",
        chordsupport=False,
        calcvibrato=False,
        vibratotypes=False,
        octaveshift=False,
        gateoffptr=False,
        filtkbtrack=False,
        pwkeybtrack=False,
        hardrestypes=False,
        frame1switch=False,
        subtunejump=False,
        subtunesupport=False,
        retainzeropage=False,
        filt_ctrl_fx=False,
        filtshift_supp=False,
        portame_notefx=False,
        vibfreqfx_supp=False,
        filter_smallfx=False,
        detune_smallfx=False,
        wfctrl_smallfx=False,
    ),
}


def features_for_driver(driver_type: int) -> PlayerFeatures:
    """The feature set for an SWM header driver byte.

    Bytes outside 0..5 are not a SID-Wizard ``PLAYERTYPE`` (a few HVSC exports
    carry a stale byte); those fall back to the default build.
    """
    return BUILD_FLAGS.get(driver_type, BUILD_FLAGS[DRIVER_NORMAL])


# Per flag, its relocation-free site in either build (1.97 player.asm lines); group 1 is the flag-on code.
_SITES: Dict[str, re.Pattern[bytes]] = {
    name: re.compile(pattern, re.S)
    for name, pattern in {
        # SETJARP (2432): ``iny ; lda (zp),y ; iny ; cmp #$7F ; beq PLYCHRD`` after SEWFARP.
        "chordsupport": rb"\x3d..\x9d..\xc8\xb1.\xc8(?:(\xc9\x7f\xf0)|\x8d|\x48)",
        # SETJARP: ``lda (zp),y ; cmp #$FF ; beq + ; sta DETUNER,x`` before ``iny ; tya ; sta WFTPOS,x``.
        "detunesupport": rb"\x3d..\x9d..\xc8\xb1.\xc8(?:\xc9\x7f\xf0.)?(?:\x8d..|\x48)"
        rb"(?:(\xb1.\xc9\xff\xf0\x03\x9d..)|)\xc8\x98\x9d",
        # SETWFTP (1856): ``lda #$FF ; sta PTNGATE,x ; sta ARPSCNT,x`` before ARPSPED / INSTVIB.
        "multispeedsupp": rb"\x9d..\xa9\xff\x9d..(?:(\x9d..)|)(?:\xa0\x07\xb1.\x9d..)?\x20",
        # WFARPTB (2396): ``dec ARPSCNT,x ; bpl SKIPWFT ; lda ARPSPED,x`` before ``and #$3F ; sta ARPSCNT,x``.
        "arpspeedsupp": rb"(?:(\xde..\x10.\xbd..)|)\x29\x3f\x9d..\xbc..\xb1.\xc9\xfe",
        # HRGTOFF (1568): HR-SR / HR-AD from the instrument (pre-1.8 walks them up), else DEFAULTHRADSR.
        "hardrestypes": rb"\xa9\xfe\x9d..\x3d..\x9d..(?:(\xa0\x02\xb1|\xc8\xb1)|\xa9)",
        # HRGTOFF's first ADSR store: a SIDG ghost rather than $D405 / $D406.
        "allghostregs": rb"\xa9\xfe\x9d..\x3d..\x9d..(?:\xa0\x02\xb1.|\xc8\xb1.|\xa9.)\x9d(?:[\x05\x06]\xd4|(..))",
        # INSCTRL (1836): ``and #8 ; beq SETWFTP`` rather than ``lda #$09 ; sta WFGHOST,x``.
        "frame1switch": rb"\x29\x30\x9d..(?:\xa9.|\xad..)(?:(\x29\x08\xf0)|\xa9\x09\x9d)",
        # NORMARP (2460): ``cmp #$80 ; beq ENDWFTB`` between ``bpl RELPTCH`` and ``cmp #$E0 ; bcc ABSPTCH``.
        "wfarp_nop_supp": rb"\x10.(?:(\xc9\x80\xf0.)|)\xc9\xe0\x90",
        # CLEGATO (1786): ``ldy #9 ; adc (zp),y`` before the transpose and ``sta DPITCH,x``.
        "octaveshift": rb"\x4c..\x18(?:(\xa0\x09\x71.)|)(?:\x18\x7d..)?\x9d",
        # CLEGATO: ``clc ; adc TRANSP,x`` after the octave shift, before ``sta DPITCH,x``.
        "transposesupp": rb"\x4c..\x18(?:\xa0\x09\x71.)?(?:(\x18\x7d..)|)\x9d",
        # STRTSND (1882): ``bit INSCTRL ; bvs ENDPWRESET`` before ``ldy #$0A ; lda (zp),y ; sta PWTPOS,x``.
        "pwresetsw": rb"(?:(\x2c..\x70.)|)\xa0\x0a\xb1.\x9d",
        # ENDPWRESET (1889): ``bit INSCTRL ; BMIauto ENDFILRESET``.
        "filtresetsw": rb"\xa0\x0a\xb1.\x9d..(?:(\x2c..(?:\x30|\x10\x03\x4c))|)",
        # PORTAME (2100): ``cpy #$FF ; beq TARGETN`` before ``ldy DPITCH,x ; lda FREQTBL,y ; sbc FREQLO,x``.
        "portavibra": rb"(?:(\xc0\xff\xf0.)|)\xbc..\xb9..\xfd..\x8d",
        # ENDPWTB (2376): ``clc ; lda PKBDTRK,x ; beq combiKT`` after SEPWPOS's ``lda #0 ; sta PWEEPCNT,x``.
        "pwkeybtrack": rb"\x98\x9d..\xa9\x00\x9d..(?:(\x18\xbd..\xf0)|\xbd)",
        # COMMONREGS: ``lda #KBTRACK ; beq ; ldx FLTCTRL`` after ``sta $D418 ; clc``.
        "filtkbtrack": rb"\x8d\x18\xd4\x18\xa9.(?:(\xf0.\xae)|)",
        # COMMONREGS: CTFLGHO's ``lda # ; sta $D415`` after ``sta $D416``.
        "finefiltsweep": rb"\x8d\x16\xd4(?:(?:\x8d..)?(\xa9.\x8d\x15\xd4)|)",
        # SETVAMP (2913): ``beq wrFmodL ; lsr ; adc DPITCH,x`` rather than LOOKUPA's ``tay ; cpy #``.
        "calcvibrato": rb"\x29\xf0\x4a(?:(\xf0.\x4a\x7d)|\xa8\xc0)",
        # CHKTMP1 (1497): ``and #$7F ; cmp #2 ; bpl + ; jmp PTN_SEQ`` before HARDRST's ``ldy CURNOT,x``.
        "fastspeedbind": rb"(?:(\x29\x7f\xc9\x02\x10\x03\x4c..)|)(?:\xa9\x02)?\xbc..\xf0.\xc0\x60\x90",
        # PLAYER's channel loop (835): ``lda #$FF ; sta TRDELAY,x`` before ``txa ; sec ; sbc #7``.
        "delaysupport": rb"(?:(\xa9\xff\x9d..)|)\x8a\x38\xe9\x07",
        # PLAYER (609): ``lda PLAYERZP ; pha ; lda PLAYERZP+1 ; pha`` before ``ldx #(3-1)*7``.
        "retainzeropage": rb"(\xa5.\x48\xa5.\x48\xa2\x0e)",
        # BIGFX1F (3758): ``pha ; and #$0F ; sta FSWITCH ; pla ; and #$F0 ; sta RESONIB``; else the label is ``rts``.
        "filt_ctrl_fx": rb"(\x48\x29\x0f\x8d..\x68\x29\xf0\x8d)",
    }.items()
}

# Sites with no flag-off anchor: their absence reads as the flag off.
_ON_ONLY = frozenset({"filt_ctrl_fx", "retainzeropage"})

# HRENDER (1593) loads the instrument pointer then ``jmp CNTPLY2|FILTPRG|SETPWID|WFARPTB``.
_INSPT = rb"\xb9..(?:\x18\x6d..)?\x85.\xb9..(?:\x6d..)?\x85."
_HRENDER = re.compile(rb"\xbc.." + _INSPT + rb"\x4c(..)", re.S)
# CNTPLAY (2058) tests CURINS before the same load; CNTPLY2 follows it.
_CNTPLAY = re.compile(rb"\xbc..\xd0\x01\x60" + _INSPT, re.S)
# WFARPTB (2396, with or without ARPSPEEDSUPP) and SETPWID's ``ldy PWTPOS,x ; lda (zp),y ; bmi``.
_NOT_CNTPLY2 = re.compile(rb"\xde..\x10.\xbd..\x29\x3f|\x29\x3f\x9d..\xbc|\xbc..\xb1.\x30", re.S)


def _vibslidealways(code: bytes, load: int) -> Optional[bool]:
    """Whether HRENDER exits to CNTPLY2, or None when its exit is not recognised."""
    hrender, cntplay = _HRENDER.search(code), _CNTPLAY.search(code)
    if hrender is None or cntplay is None:
        return None
    target = int.from_bytes(hrender.group(1), "little") - load
    if target == cntplay.end():
        return True
    return False if 0 <= target < len(code) and _NOT_CNTPLY2.match(code, target) else None


def detect_flags(code: bytes, load: int) -> Dict[str, bool]:
    """The flags whose site the player ``code`` loaded at ``load`` has, read from it."""
    found: Dict[str, bool] = {}
    for name, site in _SITES.items():
        matches = [m.group(1) is not None for m in site.finditer(code)]
        if matches or name in _ON_ONLY:
            found[name] = any(matches)
    always = _vibslidealways(code, load)
    if always is not None:
        found["vibslidealways"] = always
    return found


def detect_features(code: bytes, load: int, fallback: PlayerFeatures) -> PlayerFeatures:
    """``fallback`` with each flag :func:`detect_flags` reads from the player ``code``."""
    return replace(fallback, **detect_flags(code, load))


# The flags :func:`detect_features` can read from the code.
DETECTED_FLAGS = tuple(sorted([*_SITES, "vibslidealways"]))
