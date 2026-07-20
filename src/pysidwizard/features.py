"""SID-Wizard player build (driver-type) feature flags.

The SWM header driver byte is SID-Wizard's ``PLAYERTYPE`` (``player.asm:183``
emits it; ``SWMconvert.c:58-59`` names 0..5). The six builds are one
``player.asm`` assembled with the ``feature`` blocks below.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Dict

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
