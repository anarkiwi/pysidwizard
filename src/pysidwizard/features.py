"""SID-Wizard player variants: one facet registry keyed by ``player.asm`` site.

The SWM header driver byte is SID-Wizard's ``PLAYERTYPE`` (``player.asm:183``
emits it; ``SWMconvert.c:58-59`` names 0..5). The six builds are one
``player.asm`` assembled with the ``feature`` blocks below. A ``.sid`` export's
player code is the authority: a packed (SWP) game player is assembled from an
edited ``settings.cfg``, so each facet that has a code site is read from it
(:func:`detect_findings`) and the rest keep the driver byte's build.

:data:`FACETS` is the registry: a ``build`` facet per ``.if feature.X_ON`` flag
(its per-build default and its :class:`~pysidtracker.SitePattern`) and a
``release`` facet per release difference no flag gates. :class:`PlayerFeatures` and
:class:`PlayerQuirks` are the resolved views the player reads by attribute.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, make_dataclass, replace
from typing import Dict, Iterable, Iterator, Optional, Tuple

from pysidtracker import Facet, FacetRegistry, Finding, SitePattern
from pysidtracker.facets import Source, detected, inferred

# Driver byte values (SWMconvert.c:58-59 / altplayers.inc PLAYERTYPE lines).
DRIVER_NORMAL = 0
DRIVER_MEDIUM = 1
DRIVER_LIGHT = 2
DRIVER_EXTRA = 3
DRIVER_BARE = 4
DRIVER_DEMO = 5

BUILDS: Tuple[str, ...] = ("normal", "medium", "light", "extra", "bare", "demo")


@dataclass(frozen=True)
class PlayerSource:
    """What a detector reads: the entered player's ``code`` (loaded at ``load``) and the whole ``image``."""

    code: bytes
    load: int
    image: bytes = b""


def _on(alts: Tuple[str, ...]) -> bool:
    return "on" in alts


def _sites(pattern: SitePattern, buf: bytes) -> Iterator:
    """The non-overlapping matches of ``pattern`` in ``buf``, lowest address first."""
    pos = 0
    while True:
        hit = pattern.find_first(buf, start=pos)
        if hit is None:
            return
        yield hit
        pos = hit.addr + max(hit.length, 1)


def site_detector(site: SitePattern, *, span: str = "code", first: bool = False, absent=None):
    """Detect a flag by its ``( on: … | off: … )`` site.

    Any on-form decides (the first site only with ``first``). With no site in the
    ``span`` the facet is ``absent`` (``None`` keeps its default).
    """

    def detect(src: PlayerSource) -> Optional[Finding]:
        buf = src.image if span == "image" else src.code
        hits = list(_sites(site, buf))
        if not hits:
            if absent is None:
                return None
            return Finding(absent, inferred("site absent"))
        decisive = hits[:1] if first else hits
        on = next((h for h in decisive if _on(h.alts)), None)
        hit = on or hits[0]
        addr = src.load + hit.addr if span == "code" else None
        return Finding(on is not None, detected(addr))

    return detect


# HRENDER (1593) loads the instrument pointer then ``jmp CNTPLY2|FILTPRG|SETPWID|WFARPTB``.
_INSPT = "B9 ?? ?? [ 18 6D ?? ?? ] 85 ?? B9 ?? ?? [ 6D ?? ?? ] 85 ??"
_HRENDER = SitePattern(f"BC ?? ?? {_INSPT} 4C {{exit:w}}")
# CNTPLAY (2058) tests CURINS before the same load; CNTPLY2 follows it.
_CNTPLAY = SitePattern(f"BC ?? ?? D0 01 60 {_INSPT}")
# WFARPTB (2396, with or without ARPSPEEDSUPP) and SETPWID's ``ldy PWTPOS,x ; lda (zp),y ; bmi``.
_NOT_CNTPLY2 = SitePattern(
    "( DE ?? ?? 10 ?? BD ?? ?? 29 3F | 29 3F 9D ?? ?? BC | BC ?? ?? B1 ?? 30 )"
)


def _vibslidealways(src: PlayerSource) -> Optional[Finding]:
    """Whether HRENDER exits to CNTPLY2, or None when its exit is not recognised."""
    code = src.code
    hrender, cntplay = _HRENDER.find_first(code), _CNTPLAY.find_first(code)
    if hrender is None or cntplay is None:
        return None
    target = hrender.captures["exit"] - src.load
    addr = src.load + hrender.addr
    if target == cntplay.addr + cntplay.length:
        return Finding(True, detected(addr))
    if 0 <= target < len(code):
        hit = _NOT_CNTPLY2.find_first(code, start=target)
        if hit is not None and hit.addr == target:
            return Finding(False, detected(addr))
    return None


def _flag(
    name: str, builds: Tuple[int, ...], spec: Optional[str] = None, *, probe=None, **policy
) -> Facet:
    """A ``build`` facet: per-build defaults (normal, medium, light, extra, bare, demo) and its site.

    ``probe`` is a detector of its own for a flag read from more than one site.
    """
    detector = probe
    if spec is not None:
        detector = site_detector(SitePattern(spec), **policy)
    return Facet(
        name,
        "build",
        domain=(False, True),
        default={build: bool(on) for build, on in zip(BUILDS, builds, strict=True)},
        detector=detector,
        per_family=True,
    )


def _quirk(name: str, default: bool, spec: str, **policy) -> Facet:
    """A ``release`` facet: ``default`` unless its site is found."""
    return Facet(
        name,
        "release",
        domain=(False, True),
        default=default,
        detector=site_detector(SitePattern(spec), **policy),
    )


# SEL_INS stores a row's instrument column into CURINS only below ``cmp #maxinstamount``; exports
# set it per tune.
_SEL_INS = SitePattern("BD ?? ?? F0 ?? C9 {limit} 10 ?? 9D ?? ??")


def _sel_ins_limit(src: PlayerSource) -> Optional[Finding]:
    hit = _SEL_INS.find_first(src.code)
    if hit is None:
        return None
    return Finding(hit.captures["limit"], detected(src.load + hit.addr))


# One facet per ``feature`` flag (names are the assembly flags lower-cased without ``_ON``), in
# ``player.asm`` order of the settings.cfg:223-259 block. Per-build defaults are settings.cfg's (normal)
# and altplayers.inc's (medium 333-372, light 434-473, extra 535-574, bare 636-675 -- everything off --
# and demo 737-776). A site is the flag's relocation-free code in either build (1.97 player.asm lines);
# its ``on:`` branch is the flag-on code. Flags with the same value in every build are omitted
# (they gate nothing). SUBTUNESUPPORT_ON is a compile-time expression only bare/demo hardwire to 0.
_FLAGS = (
    #       name               normal medium light extra bare demo
    # CLEGATO: ``clc ; adc TRANSP,x`` after the octave shift, before ``sta DPITCH,x``.
    _flag(
        "transposesupp",
        (1, 1, 0, 1, 0, 1),
        "4C ?? ?? 18 [ A0 09 71 ?? ] ( on: 18 7D ?? ?? | off: ??{0} ) 9D",
    ),
    # CLEGATO (1786): ``ldy #9 ; adc (zp),y`` before the transpose and ``sta DPITCH,x``.
    _flag(
        "octaveshift",
        (1, 1, 0, 1, 0, 0),
        "4C ?? ?? 18 ( on: A0 09 71 ?? | off: ??{0} ) [ 18 7D ?? ?? ] 9D",
    ),
    # SETJARP (2432): ``iny ; lda (zp),y ; iny ; cmp #$7F ; beq PLYCHRD`` after SEWFARP.
    _flag(
        "chordsupport",
        (1, 1, 0, 1, 0, 0),
        "3D ?? ?? 9D ?? ?? C8 B1 ?? C8 ( on: C9 7F F0 | off: 8D | off: 48 )",
    ),
    # SETVAMP (2913): ``beq wrFmodL ; lsr ; adc DPITCH,x`` rather than LOOKUPA's ``tay ; cpy #``.
    _flag(
        "calcvibrato",
        (1, 1, 0, 1, 0, 0),
        "29 F0 4A ( on: F0 ?? 4A 7D | off: A8 C0 )",
    ),
    _flag("vibratotypes", (1, 0, 0, 1, 0, 0)),  # selectable vibrato types (player.asm:2087)
    # SETJARP: ``lda (zp),y ; cmp #$FF ; beq + ; sta DETUNER,x`` before ``iny ; tya ; sta WFTPOS,x``.
    _flag(
        "detunesupport",
        (1, 1, 0, 1, 0, 1),
        "3D ?? ?? 9D ?? ?? C8 B1 ?? C8 [ C9 7F F0 ?? ] ( 8D ?? ?? | 48 ) "
        "( on: B1 ?? C9 FF F0 03 9D ?? ?? | off: ??{0} ) C8 98 9D",
    ),
    # PORTAME (2100): ``cpy #$FF ; beq TARGETN`` before ``ldy DPITCH,x ; lda FREQTBL,y ; sbc FREQLO,x``.
    # Drops back to vibrato at the portamento target (2096/2118).
    _flag(
        "portavibra",
        (0, 0, 0, 1, 0, 0),
        "( on: C0 FF F0 ?? | off: ??{0} ) BC ?? ?? B9 ?? ?? FD ?? ?? 8D",
    ),
    # WFARPTB (2396): ``dec ARPSCNT,x ; bpl SKIPWFT ; lda ARPSPED,x`` before ``and #$3F ; sta ARPSCNT,x``.
    _flag(
        "arpspeedsupp",
        (1, 1, 0, 1, 0, 1),
        "( on: DE ?? ?? 10 ?? BD ?? ?? | off: ??{0} ) 29 3F 9D ?? ?? BC ?? ?? B1 ?? C9 FE",
    ),
    _flag("portame_notefx", (1, 1, 1, 1, 0, 0)),  # portamento note-FX $FF (player.asm:1529)
    _flag("gateoffptr", (1, 0, 0, 1, 0, 0)),  # gate-off WF/PW/filter table pointer
    # ENDPWTB (2376) / ENDPWRESET (1889): ``bit INSCTRL ; BMIauto ENDFILRESET``.
    _flag(
        "filtresetsw",
        (1, 1, 0, 1, 0, 1),
        "A0 0A B1 ?? 9D ?? ?? ( on: 2C ?? ?? ( 30 | 10 03 4C ) | off: ??{0} )",
    ),
    # COMMONREGS: ``lda #KBTRACK ; beq ; ldx FLTCTRL`` after ``sta $D418 ; clc``.
    _flag(
        "filtkbtrack",
        (1, 1, 0, 1, 0, 0),
        "8D 18 D4 18 A9 ?? ( on: F0 ?? AE | off: ??{0} )",
    ),
    # STRTSND (1882): ``bit INSCTRL ; bvs ENDPWRESET`` before ``ldy #$0A ; lda (zp),y ; sta PWTPOS,x``.
    _flag(
        "pwresetsw",
        (1, 1, 0, 1, 0, 1),
        "( on: 2C ?? ?? 70 ?? | off: ??{0} ) A0 0A B1 ?? 9D",
    ),
    # ENDPWTB (2376): ``clc ; lda PKBDTRK,x ; beq combiKT`` after SEPWPOS's ``lda #0 ; sta PWEEPCNT,x``.
    # PW keyboard tracking (player.asm:1914-1924).
    _flag(
        "pwkeybtrack",
        (1, 0, 0, 1, 0, 0),
        "98 9D ?? ?? A9 00 9D ?? ?? ( on: 18 BD ?? ?? F0 | off: BD )",
    ),
    # NORMARP (2460): ``cmp #$80 ; beq ENDWFTB`` between ``bpl RELPTCH`` and ``cmp #$E0 ; bcc ABSPTCH``.
    _flag(
        "wfarp_nop_supp",
        (1, 1, 1, 1, 0, 1),
        "10 ?? ( on: C9 80 F0 ?? | off: ??{0} ) C9 E0 90",
    ),
    # COMMONREGS: CTFLGHO's ``lda # ; sta $D415`` after ``sta $D416``.
    _flag(
        "finefiltsweep",
        (1, 1, 0, 1, 0, 1),
        "8D 16 D4 ( on: [ 8D ?? ?? ] A9 ?? 8D 15 D4 | off: ??{0} )",
    ),
    # HRGTOFF (1568): HR-SR / HR-AD from the instrument (pre-1.8 walks them up), else DEFAULTHRADSR.
    _flag(
        "hardrestypes",
        (1, 0, 0, 1, 0, 0),
        "A9 FE 9D ?? ?? 3D ?? ?? 9D ?? ?? ( on: ( A0 02 B1 | C8 B1 ) | off: A9 )",
    ),
    # INSCTRL (1836): ``and #8 ; beq SETWFTP`` rather than ``lda #$09 ; sta WFGHOST,x``.
    _flag(
        "frame1switch",
        (1, 0, 0, 1, 0, 0),
        "29 30 9D ?? ?? ( A9 ?? | AD ?? ?? ) ( on: 29 08 F0 | off: A9 09 9D )",
    ),
    _flag("tempoprgsupp", (1, 1, 0, 1, 0, 1)),  # tempo programs + funktempo pattern-FX
    # CHKTMP1 (1497): ``and #$7F ; cmp #2 ; bpl + ; jmp PTN_SEQ`` before HARDRST's ``ldy CURNOT,x``.
    # Collapses TICK_0..2 at tempo 1-2 (1496-1514, 1704-1723).
    _flag(
        "fastspeedbind",
        (0, 0, 0, 1, 0, 0),
        "( on: 29 7F C9 02 10 03 4C ?? ?? | off: ??{0} ) [ A9 02 ] BC ?? ?? F0 ?? C0 60 90",
    ),
    # PLAYER's channel loop (835): ``lda #$FF ; sta TRDELAY,x`` before ``txa ; sec ; sbc #7``.
    # Track/note delay $1D/$1E (3730-3753).
    _flag(
        "delaysupport",
        (0, 0, 0, 1, 0, 0),
        "( on: A9 FF 9D ?? ?? | off: ??{0} ) 8A 38 E9 07",
    ),
    _flag("subtunejump", (1, 0, 0, 1, 0, 0)),  # subtune jumping from the orderlist
    _flag("subtunesupport", (1, 1, 1, 1, 0, 0)),  # subtunes at all
    # SETWFTP (1856): ``lda #$FF ; sta PTNGATE,x ; sta ARPSCNT,x`` before ARPSPED / INSTVIB.
    _flag(
        "multispeedsupp",
        (1, 1, 1, 1, 0, 1),
        "9D ?? ?? A9 FF 9D ?? ?? ( on: 9D ?? ?? | off: ??{0} ) [ A0 07 B1 ?? 9D ?? ?? ] 20",
    ),
    # HRGTOFF's first ADSR store: a SIDG ghost rather than $D405 / $D406 (300, 443, 3278-3315).
    _flag(
        "allghostregs",
        (0, 0, 0, 1, 0, 0),
        "A9 FE 9D ?? ?? 3D ?? ?? 9D ?? ?? ( A0 02 B1 ?? | C8 B1 ?? | A9 ?? ) 9D "
        "( off: ( 05 | 06 ) D4 | on: ?? ?? )",
    ),
    # PLAYER (609): ``lda PLAYERZP ; pha ; lda PLAYERZP+1 ; pha`` before ``ldx #(3-1)*7``.
    # Saves/restores the zeropage around the player; no flag-off anchor, so absence reads off.
    _flag(
        "retainzeropage",
        (1, 0, 0, 1, 0, 0),
        "( on: A5 ?? 48 A5 ?? 48 A2 0E )",
        absent=False,
    ),
    # HRENDER's exit target (player.asm:1614-1622): :func:`_vibslidealways` and its two siblings.
    _flag("filteralways", (0, 0, 0, 1, 0, 0)),
    _flag("pulsealways", (0, 0, 0, 1, 0, 0)),
    _flag("vibslidealways", (0, 0, 0, 1, 0, 0), probe=_vibslidealways),
    # BIGFX1F (3758): ``pha ; and #$0F ; sta FSWITCH ; pla ; and #$F0 ; sta RESONIB``; else the label is
    # ``rts``. The $1F / $Bx filter-switch + resonance FX.
    _flag(
        "filt_ctrl_fx",
        (1, 0, 0, 1, 0, 0),
        "( on: 48 29 0F 8D ?? ?? 68 29 F0 8D )",
        absent=False,
    ),
    _flag("filtshift_supp", (1, 1, 1, 1, 0, 0)),  # $1C filter shift
    _flag("seq_fx_support", (1, 1, 1, 1, 0, 1)),  # orderlist FX
    _flag("volset_support", (1, 1, 1, 1, 0, 1)),  # main-volume FX
    _flag("vibfreqfx_supp", (1, 1, 1, 1, 0, 0)),  # $Dx / $16 vibrato-frequency FX
    _flag("filter_smallfx", (1, 1, 1, 1, 0, 0)),  # $Bx / $Fx small-FX
    _flag("detune_smallfx", (1, 1, 1, 1, 0, 0)),  # $8x / $9x small-FX
    _flag("wfctrl_smallfx", (1, 1, 1, 1, 0, 0)),  # $4x / $Ex small-FX
)

# Release differences in ``player.asm`` that no ``feature`` flag gates. Each default is the behaviour a
# player without the quirk's site keeps; ``first`` makes the first site decide rather than any.
_QUIRKS = (
    # Pre-1.8 drivers index STRTSND's first-frame FREQTBH load by X (the track offset) instead of
    # Y = DPITCH (ChangeLog.txt:101). INSCTRL's first-frame pitch (player.asm:1838-1843), read from the
    # whole ``.sid`` image rather than the entered player's code.
    _quirk(
        "first_frame_freq_by_track",
        False,
        "29 08 F0 ?? BC ?? ?? ( on: BD | off: B9 )",
        span="image",
        first=True,
    ),
    # V1.0's INSCTRL gives a control-bit-3 instrument a fixed $09 first frame and no FREQTBH pitch:
    # ``and #8 ; beq SETWFTP ; lda #$09 ; sta WFGHOST,x``.
    _quirk("first_frame_fixed_waveform", False, "( on: 29 08 F0 ?? A9 09 9D )"),
    # Whether SETFLTP has STOPFLT (player.asm:2003-2005): a filter table starting $FF on the
    # filter-controller track points FLTPOSI at that $FF; without it the walk goes on through the
    # track's next instrument. SETFLTP's $FF test (1993-2008) is
    # ``cmp #$FF ; beq ; stx FLTCTRL ; sty FLTPOSI ; lda FLSWTBL,x ; ora FSWITCH ; bne``, then STOPFLT's
    # ``cpx FLTCTRL``.
    _quirk(
        "filter_stop_rewinds",
        True,
        "C9 FF F0 ?? 8E {fltctrl:w} 8C ?? ?? BD ?? ?? 0D ?? ?? D0 ?? ( on: EC {fltctrl:w} | off: ??{0} )",
        first=True,
    ),
    # Whether FLADVAN reads a filter row's $80-$8F third column as FSWITCH (and a zero keyboard track)
    # rather than as the keyboard track; 1.4 on (player.asm:1827-1836): 1.4's
    # ``bpl ; cmp #$90 ; bcs ; and #$0F ; sta F_SWITCH``, else
    # ``iny ; lda (zp),y ; sta CKBDTRK+1 ; iny ; sty FLTPOSI+1``.
    _quirk(
        "kbtrack_fswitch",
        True,
        "( on: B1 ?? 10 ?? C9 90 B0 ?? 29 0F 8D | off: C8 B1 ?? 8D ?? ?? C8 [ 98 ] ( 8C | 8D ) )",
    ),
    # Whether SMALFXE stores its merged low nibble to WFGHOST (``jmp WRITEWF``, before 1.5); later
    # releases comment the store out (player.asm:2748-2753): ``lda WFGHOST,x ; jsr SETNIBL`` then
    # ``sta WFGHOST,x ; rts`` / ``jmp WRITEWF`` or a bare ``rts``, before SMALFXF's ``asl x4``.
    _quirk(
        "wfctrl_fx_stores",
        False,
        "BD ?? ?? 20 ?? ?? ( on: ( 9D ?? ?? 60 | 4C ?? ?? ) | off: 60 ) 0A 0A 0A 0A 8D",
    ),
)

_QUIRKS = (
    *_QUIRKS,
    # The instrument column value below which SEL_INS selects an instrument (player.asm:1777,
    # ``cmp #maxinstamount ; bpl``); a larger value leaves CURINS as it was.
    Facet("sel_ins_limit", "release", domain=range(0x100), default=0x3F, detector=_sel_ins_limit),
)

FACETS = FacetRegistry([*_FLAGS, *_QUIRKS])

FLAG_FACETS: Tuple[Facet, ...] = FACETS.of_kind("build")
QUIRK_FACETS: Tuple[Facet, ...] = FACETS.of_kind("release")


def _flag_names(self) -> Dict[str, bool]:
    """The flag fields as an ``{ASSEMBLY_NAME_ON: value}`` mapping."""
    return {f.name.upper() + "_ON": getattr(self, f.name) for f in fields(self) if f.type is bool}


# The resolved views: plain frozen dataclasses so the player reads ``features.X`` / ``quirks.X``.
PlayerFeatures = make_dataclass(
    "PlayerFeatures",
    [("driver_type", int), ("name", str)] + [(f.name, bool) for f in FLAG_FACETS],
    frozen=True,
    namespace={
        "__doc__": "One build's compile-time ``feature`` block (a flag per :data:`FLAG_FACETS` facet).",
        "flag_names": _flag_names,
    },
)
PlayerQuirks = make_dataclass(
    "PlayerQuirks",
    [(f.name, type(f.default), field(default=f.default)) for f in QUIRK_FACETS],
    frozen=True,
    namespace={"__doc__": "Release differences in ``player.asm`` that no ``feature`` flag gates."},
)
PlayerFeatures.__module__ = PlayerQuirks.__module__ = __name__


BUILD_FLAGS: Dict[int, PlayerFeatures] = {
    driver: PlayerFeatures(
        driver_type=driver, name=build, **{f.name: f.default_for(build) for f in FLAG_FACETS}
    )
    for driver, build in enumerate(BUILDS)
}


def features_for_driver(driver_type: int) -> PlayerFeatures:
    """The feature set for an SWM header driver byte.

    Bytes outside 0..5 are not a SID-Wizard ``PLAYERTYPE`` (a few HVSC exports
    carry a stale byte); those fall back to the default build.
    """
    return BUILD_FLAGS.get(driver_type, BUILD_FLAGS[DRIVER_NORMAL])


def detect_findings(
    code: bytes, load: int, image: bytes = b"", facets: Iterable[Facet] = FACETS
) -> Dict[str, Finding]:
    """Every one of ``facets`` with a site in the player ``code`` (loaded at ``load``) or ``image``."""
    src = PlayerSource(code, load, image)
    found: Dict[str, Finding] = {}
    for facet in facets:
        finding = facet.detector(src) if facet.detector is not None else None
        if finding is not None:
            found[facet.name] = finding
    return found


def detect_flags(code: bytes, load: int) -> Dict[str, bool]:
    """The flags whose site the player ``code`` loaded at ``load`` has, read from it."""
    return {
        name: finding.value
        for name, finding in detect_findings(code, load, b"", FLAG_FACETS).items()
    }


def detect_features(code: bytes, load: int, fallback: PlayerFeatures) -> PlayerFeatures:
    """``fallback`` with each flag :func:`detect_flags` reads from the player ``code``."""
    return replace(fallback, **detect_flags(code, load))


def detect_quirks(code: bytes, image: bytes) -> PlayerQuirks:
    """The quirks of the player ``code`` (in ``.sid`` ``image``), read from their sites."""
    found = detect_findings(code, 0, image, QUIRK_FACETS)
    return PlayerQuirks(**{f.name: found[f.name].value for f in QUIRK_FACETS if f.name in found})


def detect_variant(
    code: bytes, load: int, image: bytes, fallback: PlayerFeatures
) -> Tuple[PlayerFeatures, PlayerQuirks, Dict[str, Source]]:
    """``(features, quirks, provenance)`` read from a ``.sid``'s player ``code`` and whole ``image``.

    ``fallback`` keeps each flag with no site; ``provenance`` maps each facet read from the code to
    its :class:`~pysidtracker.facets.Source`.
    """
    found = detect_findings(code, load, image)
    flags = {f.name: found[f.name].value for f in FLAG_FACETS if f.name in found}
    quirks = {f.name: found[f.name].value for f in QUIRK_FACETS if f.name in found}
    sources = {name: finding.source for name, finding in found.items()}
    return replace(fallback, **flags), PlayerQuirks(**quirks), sources


# The flags :func:`detect_features` can read from the code.
DETECTED_FLAGS = tuple(sorted(f.name for f in FLAG_FACETS if f.detector is not None))
