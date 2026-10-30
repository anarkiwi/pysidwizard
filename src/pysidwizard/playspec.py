"""The SID-Wizard playroutine as a ``pysidtracker.playspec`` spec.

``specs/sidwizard.play`` states the play call; :func:`binding` matches each routine where
the play entry's control flow reaches it, its pattern assembled from the build's flags as
``player.asm`` assembles it, and lays the per-voice variables out from VARIABLES.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import asdict
from importlib import resources
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from pysidtracker import CodePattern, SidImage, code_match_at
from pysidtracker.playspec import parse as parse_spec

from .features import DRIVER_NORMAL, FLAG_FACETS, detect_variant, features_for_driver

SPEC = parse_spec(resources.files(__package__).joinpath("specs/sidwizard.play").read_text())

VOICE_STRIDE = 7
CHANNELS = 3

# The per-voice variable bunches in ``player.asm`` order (165-310), each gated by None or a key.
_BUNCHES: Tuple[Tuple[Optional[str], Tuple[str, ...]], ...] = (
    (None, ("freqlo", "freqhi", "pwlogho", "pwhigho", "wfghost", "ptngate", "pweepcnt")),
    (None, ("packcnt", "spdcnt", "seqpos", "ptnpos", "wftpos", "pwtpos", "arpscnt")),
    (None, ("curptn", "curnot", "dpitch", "curifx", "curins", "curfx2", "curval")),
    (None, ("slidevib", "freqmodl", "freqmodh", "videlcnt", "vibfrequ", "vibracnt", "transp")),
    ("tmpbase", ("tmpbase", "tmppos", "arpsped", "pkbdtrk", "curchord", "chordpos", "arpdpitch")),
    ("sidg", ("sidg",)),
)
# CONST_VAR's bunch: the filter-switch masks and track-tempo slots, then four variables.
_CONST_BUNCH = ("flswtbl", "flswtb2", "trktmpos", "trdelay", "seqtempo", "transp2", "detuner")
# The order PLAYER runs the voices in: the last voice of each chip first.
_ORDER = tuple(v for v in (2, 5, 8, 11, 1, 4, 7, 10, 0, 3, 6, 9) if v < CHANNELS)
# Layout fields only some builds have: 0 where a build leaves the code out.
_OPTIONAL = (
    "tempotbl",
    "tempo",
    "p_tmpt2",
    "p_tmpt3",
    "p_tmpt6",
    "p_tmpt7",
    "p_tmpt8",
    "p_tmpt9",
    "p_tmpp1",
    "p_tmpp2",
    "p_chdt1",
    "p_chdt2",
    "p_chdp1",
    "p_chdp2",
    "p_chdp3",
    "p_chdp4",
    "p_subt1",
    "p_subt2",
    "p_insl1",
    "p_insh1",
    "p_insl2",
    "p_insh2",
    "exptabh",
    "tablrst",
    "ckbdtrk",
    "flshift",
    "ctflgho",
    "seqvolu",
    "xstore",
    "subtpos",
    "tmpbase",
    "tmppos",
    "arpsped",
    "pkbdtrk",
    "curchord",
    "chordpos",
    "sfxleng",
    "sfxtimer",
    "curinstemp",
    "swp_offset",
    "mulcntp",
)
# Release differences the binder reads from the routine it matches.
_RELEASE = (
    "hr_ad_first",
    "adsr_ad_first",
    "sfx",
    "swp",
    "wfctrl_jmp",
    "seq_tempo_capped",
    "table_fx_resets",
)
_ALT = re.compile(r"<(\w+):([^<|>]*)(?:\|([^<>]*))?>")
_ALT_NAME = re.compile(r"<(\w+):")
_ZP_SAVE = "<retainzeropage: A5 {zp} 48 A5 ?? 48 |> "
ROW_READER_TEXT = (
    "BC {ptnpos:w} BD {packcnt:w} D0 ?? B1 {zp_note} C9 70 90 ?? C9 78 B0 ?? E9 6D "
    "9D {packcnt:w} DE {packcnt:w} A9 00 C9 00 30 ?? 9D {curnot:w} 10 ?? 29 7F 9D {curnot:w} "
    "C8 B1 {zp_inst} 30 ?? 9D {curifx:w} 10 ?? 29 7F 9D {curifx_long:w} C8 B1 {zp_fx} "
    "9D {curfx2:w} 29 E0 D0 ?? C8 B1 {zp_value} 9D {curval:w} 98 9D {ptnpos:w}"
)
_PLAYER = (
    _ZP_SAVE + "A2 0E <sfx: AD {sfxtimer_ref:w} F0 0D 30 17 CD {sfxleng_ref:w} D0 0F "
    "20 {chknote:w} 4C ?? ?? AC {curinstemp_ref:w} 8C {curins_v2:w} 20 {sfx_hr:w} "
    "CE {sfxtimer_ref:w} |> 20 {dotrack:w} A2 07 20 {dotrack:w} A2 00 20 {dotrack:w}"
)
# A hand-edited PLAYER entry that toggles its own immediate and returns on alternate calls.
_GATE = "A9 {at_gate} 49 {gate_mask} 8D {gate_ref:w} {gate_branch} 01 60"
_MULPLY = _ZP_SAVE + (
    "A2 0E 20 {mulcntp:w} A2 07 20 {mulcntp:w} A2 00 20 {mulcntp:w} 4C {commonregs:w}"
)


def image(view) -> SidImage:
    """The 64 KiB ``view`` as a :class:`~pysidtracker.SidImage`."""
    return SidImage.from_prg(b"\0\0" + bytes(view))


def _s8(value: int) -> int:
    return value - 0x100 if value & 0x80 else value


def _pointer(table: str, swp: str = "") -> str:
    """``lda TABLE_lo,y ; sta zp ; lda TABLE_hi,y ; sta zp+1``, with an SWP export's offset."""
    lo, hi = f"p_{table}l{swp}", f"p_{table}h{swp}"
    return (
        f"B9 {{{lo}:w}} <swp: 18 6D {{swp_offset:w}} |> 85 {{zp}} "
        f"B9 {{{hi}:w}} <swp: 6D {{swp_offset_hi:w}} |> 85 ??"
    )


class _Scan:
    """Anchored matches of the player's routines and their captures.

    ``{name}`` records an operand, ``{at_name}`` its address, ``{to_name}`` a branch's target
    and ``{name_ref}`` a reference to ``name``; ``<flag: on | off>`` is the code a build flag
    assembles, an unknown flag taking the value whose code is there. Captures must agree.
    """

    def __init__(self, view, flags: Dict[str, Optional[bool]]):
        self.view = bytes(view)
        self.image = image(view)
        self.flags = flags
        self.found: Dict[str, int] = {}

    def word(self, addr: int) -> int:
        """The little-endian word at ``addr``."""
        return self.view[addr] | self.view[(addr + 1) & 0xFFFF] << 8

    def _variants(self, text: str) -> Iterator[Tuple[Dict[str, bool], str]]:
        unknown = sorted({n for n in _ALT_NAME.findall(text) if self.flags[n] is None})
        for values in itertools.product((True, False), repeat=len(unknown)):
            choice = dict(zip(unknown, values, strict=True))
            flags = {**self.flags, **choice}
            body = text
            while _ALT.search(body):
                body = _ALT.sub(
                    lambda m, f=flags: m.group(2) if f[m.group(1)] else m.group(3) or "", body
                )
            yield choice, body

    def match(self, routine: str, addr: int, text: str) -> Optional[int]:
        """Match ``text`` at ``addr``; the address after the match, or None."""
        for choice, body in self._variants(text):
            if not body.split():
                self.flags.update(choice)
                return addr
            pattern = CodePattern(" ".join(body.split()))
            if code_match_at(self.image, pattern, addr) is not None:
                self.flags.update(choice)
                for name, offset, width in pattern.captures:
                    self._capture(routine, name, (addr + offset) & 0xFFFF, width)
                return addr + pattern.length
        return None

    def at(self, routine: str, addr: int, text: str) -> int:
        """:meth:`match` that raises when the routine is not there."""
        end = self.match(routine, addr, text)
        if end is None:
            body = next(self._variants(text))[1]
            pattern = CodePattern(" ".join(body.split()))
            off, want = next(
                ((o, v) for o, v in pattern.literals if self.view[(addr + o) & 0xFFFF] != v),
                (0, 0),
            )
            raise ValueError(
                f"no SID-Wizard {routine} at ${addr:04x} for this build: "
                f"${(addr + off) & 0xFFFF:04x} holds ${self.view[(addr + off) & 0xFFFF]:02x},"
                f" not ${want:02x}"
            )
        return end

    def _capture(self, routine: str, name: str, where: int, width: int) -> None:
        value = self.word(where) if width == 2 else self.view[where]
        if name.startswith("at_"):
            name, value = name[3:], where
        elif name.startswith("to_"):
            value = (where + 1 + _s8(value)) & 0xFFFF
        elif name.endswith("_ref"):
            name = name[:-4]
        self.put(routine, name, value)

    def put(self, routine: str, name: str, value: int) -> None:
        """Record ``name``; a different earlier value is an inconsistent build."""
        if self.found.setdefault(name, value) != value:
            raise ValueError(
                f"SID-Wizard {routine} addresses {name} at ${value:04x}, the player at "
                f"${self.found[name]:04x}: a partly relocated player is not stated"
            )


def _commonregs(scan: _Scan, addr: int) -> None:
    """COMMONREGS (1007) and RETPLAY (1229)."""
    if scan.match("V1.0's SUBTJMP", addr, "A9 ?? F0 0D 29 7F 20 ?? ?? 20") is not None:
        raise ValueError(
            "SID-Wizard 1.0 players (subtune jumps from COMMONREGS, STRTSND in another order)"
            " are not stated by the spec"
        )
    ghost = 20 if scan.flags["allghostregs"] else 0
    if scan.match("an ECHO build", addr + ghost + 9, "09 ?? 18") is not None:
        raise ValueError("SID-Wizard echo builds (ECHO_SUPPORT) are not stated by the spec")
    at = scan.at(
        "COMMONREGS",
        addr,
        "<allghostregs: A2 0E 38 BD {sidg_sr:w} 9D {sid_sr:w} BD ?? ?? 9D ?? ?? BD {sidg:w} "
        "9D ?? ?? BD ?? ?? 9D ?? ?? BD ?? ?? 9D ?? ?? BD ?? ?? 9D ?? ?? BD ?? ?? 9D ?? ?? "
        "8A E9 07 AA B0 ?? |> A9 {at_fswitch} 09 {at_resonib} 8D {sid_resfc:w} A9 {at_mainvol} "
        "09 {at_fltband} 8D {sid_fmvol:w} 18 <filtkbtrack: A9 {at_ckbdtrk} F0 0A AE {fltctrl_ref:w} "
        "7D {dpitch:w} A8 B9 {exptabh:w} 69 {at_ctfhgho} | A9 {at_ctfhgho} > "
        "<filtshift_supp: 69 {at_flshift} |> 8D {sid_fcut_hi:w} "
        "<finefiltsweep: A9 {at_ctflgho} 8D {sid_fcut_lo:w} |> "
        "",
    )
    scan.put("RETPLAY", "retplay", at)
    scan.at("RETPLAY", at, "<retainzeropage: 68 85 ?? 68 85 {zp} |> 60")


def _mulply(scan: _Scan, entry: int) -> None:
    """MULPLY (1240): each voice's MULCNTP in schedule order, then COMMONREGS."""
    scan.at("MULPLY", entry, _MULPLY)
    scan.at(
        "MULCNTP",
        scan.found["mulcntp"],
        "BD {arpscnt:w} 30 ?? BC {curins:w} " + _pointer("ins", "1") + " A0 07 B1 {zp} 30 ?? "
        "29 40 D0 03 4C {wfarptb:w} 4C {setpwid:w} 4C {filtprg:w}",
    )


def _dotrack(scan: _Scan) -> None:
    """DOTRACK (1333): the tick clock, TICK_0 with the row reader, TICK_1 and PTN_SEQ."""
    at = scan.at(
        "DOTRACK",
        scan.found["dotrack"],
        "<tempoprgsupp: BC {tmppos:w} BD {spdcnt:w} C8 38 F9 {tempotbl:w} F0 07 50 0C "
        "BC {tmpbase:w} A9 00 9D {spdcnt:w} 98 9D {tmppos:w} | BD {spdcnt:w} 38 ED {tempo:w} "
        "F0 04 50 05 A9 00 9D {spdcnt:w} > BD {spdcnt:w} FE {spdcnt:w} C9 00 F0 03 4C {chktck1:w}",
    )
    scan.put("DOTRACK", "tick_0", at)
    at = scan.at(
        "TICK_0",
        at,
        "BC {curptn:w} " + _pointer("ptn", "1") + " A9 00 9D {curifx:w} 9D {curfx2:w} "
        "<seq_fx_support: A9 {at_seqvolu} 8D {mainvol_ref:w} |> "
        "<tempoprgsupp: BD {seqtempo:w} F0 08 20 {traktmp:w} A9 00 9D {seqtempo:w} |>",
    )
    scan.put("TICK_0", "read_row", at)
    at = scan.at("the row reader", at, ROW_READER_TEXT)
    scan.put("the row reader", "reader_end", at)
    at = scan.at(
        "the SFX row",
        at,
        "<sfx: E0 0E D0 1F AD {sfxtimer_ref:w} 30 1A A9 00 8D {curfx2_v2:w} 8D {curnot_v2:w} "
        "AC {curifx_v2:w} F0 0D C0 3F B0 03 8C {curinstemp_ref:w} 8D {curifx_v2:w} 4C {hrender:w} |>"
        "<fastspeedbind: BC {tmppos:w} B9 {p_tmpt2:w} 29 7F C9 02 10 03 4C {ptn_seq:w} |> "
        "<hardrestypes: A9 02 |>",
    )
    scan.put("HARDRST", "hardrst", at)
    _hardrst(scan, at)
    at = scan.at(
        "CHKTCK1",
        scan.found["chktck1"],
        "C9 02 10 {to_chktck2} BC {curptn:w} " + _pointer("ptn", "2"),
    )
    scan.put("TICK_1", "ptn_seq", at)
    at = scan.at(
        "PTN_SEQ",
        at,
        "BC {ptnpos:w} <transposesupp: BD {transp2:w} 9D {transp:w} |> BD {packcnt:w} D0 ?? "
        "C8 B1 {zp} C9 FF D0 ?? BC {seqpos:w} C8 20 {seqsub:w} 10 ?? C9 FE D0 06 DE {spdcnt:w} "
        "4C {cntplay:w} C9 FF <seq_fx_support: F0 0D 20 {seq_fx:w} C8 20 {seqsub:w} C9 FF F0 ?? "
        "D0 ?? | D0 ?? > C8 20 {seqsub:w} 10 ?? <subtunejump: 20 {setseqa:w} |> A9 00 A8 4C ?? ?? "
        "9D {curptn:w} 98 9D {seqpos:w} A0 00 98 9D {ptnpos:w} "
        "<fastspeedbind: BC {tmppos:w} B9 {p_tmpt3:w} 29 7F C9 03 30 {to_tick_2} |> "
        "<hardrestypes: A9 01 |> 4C {hardrst:w}",
    )
    at = scan.at("CHKTCK2", scan.found["to_chktck2"], "F0 03 4C {cntplay:w}")
    scan.put("CHKTCK2", "tick_2", at)
    _tick_2(scan, at)


def _hardrst(scan: _Scan, at: int) -> None:
    """HARDRST/NEWNOTE/HRGTOFF (1520-1592) and HRENDER."""
    hr_adsr = (
        "<hr_ad_first: C8 B1 {zp} 9D {sidg_ad:w} C8 B1 {zp} 9D {sidg_sr:w} A0 00 B1 {zp} "
        "| A0 02 B1 {zp} 9D {sidg_sr:w} 88 B1 {zp} 9D {sidg_ad:w} 88 B1 {zp} > "
    )
    at = scan.at(
        "HARDRST",
        at,
        "BC {curnot:w} F0 04 C0 60 90 03 4C {cntplay:w} BC {curfx2:w} C0 03 F0 ?? "
        "<portame_notefx: BC {slidevib:w} C8 F0 ?? |> BC {curifx:w} "
        "<hardrestypes: F0 06 C0 3F F0 ?? 30 03 BC {curins:w} 48 " + _pointer("ins", "2") + " 68 "
        "A0 00 31 {zp} F0 ?? A9 FE 9D {ptngate:w} 3D {wfghost:w} 9D {wfghost:w} " + hr_adsr + ""
        "29 04 F0 ?? A9 18 9D {wfghost:w} 9D {sidg_wave:w} 60 "
        "| C0 3F F0 ?? A9 FE 9D {ptngate:w} 3D {wfghost:w} 9D {wfghost:w} "
        "A9 0F 9D {sidg_ad:w} A9 00 9D {sidg_sr:w} >",
    )
    if scan.found.setdefault("hrender", at) != at:
        raise ValueError("SID-Wizard HRENDER is not where HARDRST ends")
    scan.at("HRENDER", at, "BC {curins:w} " + _pointer("ins", "3") + " 4C {hr_exit:w}")


def _tick_2(scan: _Scan, at: int) -> None:
    """TICK_2 to SETADSR (1736-2056)."""
    scan.put("TICK_2", "chknote", at + (16 if scan.flags["tablereset"] else 12))
    scan.at(
        "TICK_2",
        at,
        "<tablereset: A0 FF |> BD {curifx:w} F0 ?? C9 {sel_ins_limit} 10 ?? 9D {curins:w} "
        "<tablereset: A0 3F 8C {tablrst_ref:w} |> BC {curins:w} D0 03 4C {legatoo:w} "
        + _pointer("ins", "4")
        + " BD {curnot:w} D0 06 20 {insptfx:w} 4C {cntply2:w} C9 60 30 06 20 {note_fx:w} "
        "4C {cntply2:w} 18 <octaveshift: A0 09 71 {zp} |> <transposesupp: 18 7D {transp:w} |> "
        "9D {dpitch:w} A9 03 DD {curfx2:w} F0 ?? <portame_notefx: 7D {slidevib:w} B0 ?? |> "
        "A9 3F DD {curifx:w} D0 {to_strtsnd} A9 7F 9D {freqmodh:w} A9 83 9D {slidevib:w} "
        "20 {insptfx:w} 4C {wrwfgho:w}",
    )
    q = scan.flags
    if not q["frame1switch"]:
        frame1 = "A9 09 9D {wfghost:w}"
    elif q["first_frame_fixed_waveform"]:
        frame1 = "29 08 F0 05 A9 09 9D {wfghost:w}"
    else:
        freq = "BD" if q["first_frame_freq_by_track"] else "B9"
        frame1 = (
            f"29 08 F0 10 BC {{dpitch:w}} {freq} {{freqtbh_f1:w}} 9D {{sidg_freqhi:w}} "
            "A0 0F B1 {zp} 9D {wfghost:w}"
        )
    at = scan.at(
        "STRTSND",
        scan.found["to_strtsnd"],
        "A0 00 B1 {zp} <tablereset: 29 {at_tablrst} |> 8D {insctrl_ref:w} 29 30 "
        f"9D {{slidevib:w}} A9 {{at_insctrl}} {frame1} A9 10 9D {{wftpos:w}} A9 FF "
        "9D {ptngate:w} <multispeedsupp: 9D {arpscnt:w} |> "
        "<arpspeedsupp: A0 07 B1 {zp} 9D {arpsped:w} |> 20 {setvib0:w} "
        "<chordsupport: A0 08 B1 {zp} 9D {curchord:w} A8 B9 {p_chdp1:w} 9D {chordpos:w} |> "
        "<pwresetsw: 2C {insctrl_ref:w} 70 ?? |> A0 0A B1 {zp} 9D {pwtpos:w} "
        "<filtresetsw: 2C {insctrl_ref:w} 30 ?? |> A0 0B B1 {zp} A8 B1 {zp} F0 ?? C9 FF F0 ?? "
        "8E {fltctrl_ref:w} 8C {fltposi_ref:w} BD {flswtbl:w} 0D {fswitch_ref:w} D0 ?? "
        "<filter_stop_rewinds: EC {fltctrl_ref:w} D0 03 8C {fltposi_ref:w} |> BD {flswtb2:w} "
        "2D {fswitch_ref:w} 8D {fswitch_ref:w}",
    )
    at = scan.at(
        "SETADSR",
        at,
        "<adsr_ad_first: A0 03 B1 {zp} 9D {sidg_ad:w} C8 B1 {zp} 9D {sidg_sr:w} "
        "| A0 04 B1 {zp} 9D {sidg_sr:w} 88 B1 {zp} 9D {sidg_ad:w} >",
    )
    scan.put("SETADSR", "setadsr_fx", at)
    scan.at("SETADSR", at, "20 {insptfx:w} 4C {wrwfgho:w}")


def _cntplay(scan: _Scan) -> None:
    """CNTPLAY/VIBSLIDE (2058-2172), FILTPRG and SETPWID."""
    at = scan.at("CNTPLAY", scan.found["cntplay"], "BC {curins:w} D0 01 60 " + _pointer("ins", "5"))
    scan.put("CNTPLAY", "cntply2", at)
    at = scan.at(
        "VIBSLIDE",
        at,
        "BC {slidevib:w} <vibratotypes: F0 ?? |> 10 ?? C0 82 F0 ?? 10 03 4C ?? ?? "
        "<portavibra: C0 FF F0 ?? |> BC {dpitch:w} B9 {freqtbl:w} FD {freqlo:w} 8D {storfrl_ref:w} "
        "B9 {freqtbh:w} FD {freqhi:w} A8 A9 {at_storfrl} B0 ?? 7D {freqmodl:w} 98 7D {freqmodh:w} "
        "90 ?? <portavibra: 20 {forcvib:w} 20 {setvib1:w} |> BC {dpitch:w} B9 {freqtbl:w} "
        "9D {freqlo:w} B9 {freqtbh:w} 4C ?? ?? BC {videlcnt:w} 30 ?? DE {videlcnt:w} 4C {filtprg:w} "
        "<vibratotypes: BD {freqmodl:w} 18 7D {videlcnt:w} 9D {freqmodl:w} 90 03 FE {freqmodh:w} |>"
        " BD {vibracnt:w} D0 03 BD {vibfrequ:w} 38 E9 01 9D {vibracnt:w} 0A DD {vibfrequ:w} 90 ?? "
        "BD {freqlo:w} FD {freqmodl:w} 9D {freqlo:w} BD {freqhi:w} FD {freqmodh:w} 4C ?? ?? "
        "FD {freqmodl:w} 98 FD {freqmodh:w} 90 ?? BD {freqlo:w} 7D {freqmodl:w} 9D {freqlo:w} "
        "BD {freqhi:w} 7D {freqmodh:w} 9D {freqhi:w}",
    )
    scan.put("VIBSLIDE", "filtprg", at)
    kbtrack = ""
    if scan.flags["filtkbtrack"]:
        kbtrack = (
            "B1 {zp} "
            + (
                "10 ?? C9 90 B0 ?? 29 0F 8D {fswitch_ref:w} A9 00 "
                if scan.flags["kbtrack_fswitch"]
                else ""
            )
            + "8D {ckbdtrk_ref:w} C8 "
        )
    at = scan.at(
        "FILTPRG",
        at,
        "E0 {at_fltctrl} D0 ?? A0 {at_fltposi} B1 {zp} 30 ?? C8 C9 {at_cwepcnt} F0 ?? "
        "EE {cwepcnt_ref:w} 18 B1 {zp} <finefiltsweep: 10 ?? 09 F8 6D {ctflgho_ref:w} 08 29 07 "
        "8D {ctflgho_ref:w} B1 {zp} 49 FF 4A 4A 4A 49 FF 4C ?? ?? 29 07 6D {ctflgho_ref:w} C9 08 "
        "08 29 07 8D {ctflgho_ref:w} B1 {zp} 4A 4A 4A 28 |> 6D {ctfhgho_ref:w} 8D {ctfhgho_ref:w} "
        "4C {setpwid:w} C9 FE F0 04 90 ?? B0 ?? C8 B1 {zp} CD {fltposi_ref:w} F0 ?? A8 B1 {zp} "
        "10 ?? 48 29 70 8D {fltband_ref:w} 68 0A 0A 0A 0A 8D {resonib_ref:w} C8 B1 {zp} "
        "8D {ctfhgho_ref:w} <finefiltsweep: A9 00 8D {ctflgho_ref:w} |> C8 "
        + kbtrack
        + "<filtkbtrack: | C8 > 8C {fltposi_ref:w} A9 00 8D {cwepcnt_ref:w}",
    )
    scan.put("FILTPRG", "setpwid", at)
    at = scan.at(
        "SETPWID",
        at,
        "BC {pwtpos:w} B1 {zp} 30 ?? C8 DD {pweepcnt:w} F0 ?? FE {pweepcnt:w} B1 {zp} 10 03 "
        "DE {pwhigho:w} 18 7D {pwlogho:w} 9D {pwlogho:w} 90 ?? FE {pwhigho:w} B0 ?? "
        "C9 FE F0 04 90 ?? B0 ?? C8 B1 {zp} DD {pwtpos:w} F0 ?? A8 B1 {zp} 10 ?? 29 7F "
        "9D {pwhigho:w} C8 B1 {zp} 9D {pwlogho:w} C8 <pwkeybtrack: B1 {zp} 9D {pkbdtrk:w} |> "
        "C8 98 9D {pwtpos:w} A9 00 9D {pweepcnt:w} <pwkeybtrack: 18 BD {pkbdtrk:w} F0 0A "
        "7D {dpitch:w} A8 B9 {exptabh:w} F9 {exptabh_m1:w} 7D {pwhigho:w} | BD {pwhigho:w} > "
        "9D {sidg_pwhi:w} BD {pwlogho:w} 9D {sidg_pwlo:w}",
    )
    scan.put("SETPWID", "wfarptb", at)
    _wfarptb(scan, at)


def _wfarptb(scan: _Scan, at: int) -> None:
    """WFARPTB to RETRACK (2396-2609)."""
    at = scan.at(
        "WFARPTB",
        at,
        "<arpspeedsupp: DE {arpscnt:w} 10 ?? BD {arpsped:w} |> 29 3F 9D {arpscnt:w} "
        "BC {wftpos:w} B1 {zp} C9 FE F0 ?? 90 03 4C {endwftb:w} "
        "<arpspeedsupp: C9 10 B0 ?? 9D {arpscnt:w} 90 ?? | 90 ?? > C8 B1 {zp} 30 ?? 9D {wftpos:w} "
        "A8 B1 {zp} 3D {ptngate:w} 9D {wfghost:w} C8 B1 {zp} C8 <chordsupport: C9 7F F0 ?? |> "
        "8D {astorez_ref:w} <detunesupport: B1 {zp} C9 FF F0 03 9D {detuner:w} |> C8 98 "
        "9D {wftpos:w} A9 {at_astorez} 10 ?? <wfarp_nop_supp: C9 80 F0 ?? |> C9 E0 90 ?? "
        "<chordsupport: B0 ?? B1 {zp} 9D {detuner:w} BC {chordpos:w} B9 {p_chdt1:w} C9 7E D0 ?? "
        "BC {curchord:w} B9 {p_chdp2:w} 9D {chordpos:w} BD {wftpos:w} 69 02 9D {wftpos:w} "
        "4C ?? ?? C9 7F D0 ?? BC {curchord:w} B9 {p_chdp3:w} 9D {chordpos:w} A8 B9 {p_chdt2:w} "
        "FE {chordpos:w} |> 18 7D {dpitch:w} 29 7F A8 B9 {freqtbl:w} 9D {freqlo:w} "
        "B9 {freqtbh:w} 9D {freqhi:w}",
    )
    if scan.found["endwftb"] != at:
        raise ValueError(f"SID-Wizard WFARPTB: ENDWFTB is not at ${at:04x}")
    at = scan.at(
        "WRPITCH",
        at,
        "BD {freqlo:w} <detunesupport: 7D {detuner:w} |> 9D {sidg:w} BD {freqhi:w} "
        "<detunesupport: 69 00 |> 9D {sidg_freqhi:w}",
    )
    scan.put("WRPITCH", "wrwfgho", at)
    scan.at("WRWFGHO", at, "BD {wfghost:w} 9D {sidg_wave:w} 60")


def _subroutines(scan: _Scan) -> None:
    """SEQSUB, SETSEQA, SEQ_FX, SETVIB0 to SETFMOD, NOTE_FX, INSPTFX and SMALPFX."""
    scan.at(
        "SEQSUB",
        scan.found["seqsub"],
        "E0 07 F0 ?? 10 ?? B9 {at_p_seqt1:w} 60 B9 ?? ?? 60 B9 ?? ?? 60",
    )
    if scan.flags["subtunejump"]:
        scan.at(
            "SETSEQA",
            scan.found["setseqa"],
            "8E {xstore_ref:w} 0A 0A 0A 8D {subtpos_ref:w} BD {trktmpos:w} 0A E9 02 AA 4A 18 "
            "69 {at_subtpos} A8 B9 {p_subt1:w} 9D ?? ?? C8 B9 {p_subt2:w} 9D ?? ?? "
            "A2 {at_xstore} 60",
        )
    if scan.flags["seq_fx_support"]:
        scan.at(
            "SEQ_FX",
            scan.found["seq_fx"],
            "C9 A0 <seq_tempo_capped: B0 | 10 > ?? <transposesupp: E9 8F 9D {transp2:w} |> 60 "
            "C9 B0 <seq_tempo_capped: B0 | 10 > ?? 29 0F 8D {seqvolu_ref:w} 60 "
            "<seq_tempo_capped: C9 F0 B0 ?? <tempoprgsupp: E9 AF 9D {seqtempo:w} |> "
            "| <tempoprgsupp: E9 B0 9D {seqtempo:w} |> > 60",
        )
    at = scan.at(
        "SETVIB0",
        scan.found["setvib0"],
        "A0 06 B1 {zp} 9D {videlcnt:w} A0 05 B1 {zp} 48 29 0F 0A 9D {vibfrequ:w} 4A "
        "<vibratotypes: BC {slidevib:w} C0 20 10 01 |> 4A <vibratotypes: C0 30 D0 02 A9 00 |> "
        "9D {vibracnt:w} 68 29 F0 4A",
    )
    scan.put("SETVIB0", "setfmod", at)
    scan.put("SETVIB0", "setvib1", scan.found["setvib0"] + 7)
    scan.put("SETVIB0", "setvibr", scan.found["setvib0"] + 11)
    scan.at(
        "SETFMOD",
        at,
        "<calcvibrato: F0 ?? 4A 7D {dpitch:w} |> A8 C0 {exp_end} B0 ?? C0 {exp_threshold} B0 ?? "
        "B9 {exptbase:w} 9D {freqmodl:w} A9 00 F0 ?? A0 {exp_end} B9 {vib_lo:w} 9D {freqmodl:w} "
        "B9 {vib_hi:w} 9D {freqmodh:w} 60",
    )
    at = scan.at(
        "NOTE_FX",
        scan.found["note_fx"],
        "C9 78 10 09 8D {valstor_ref:w} 20 {vibamfx:w} 4C {insptfx:w} A8 B9 {notefxtbl:w} "
        "8D {indexj1_ref:w} BD {wfghost:w} 18 90 {at_indexj1}",
    )
    scan.put("NOTE_FX", "indexj1_end", at)
    scan.put("NOTE_FX", "selntfx", scan.found["note_fx"] + 13)
    scan.put("INSPTFX", "bigptfx", scan.found["insptfx"] + 19)
    at = scan.at(
        "INSPTFX",
        scan.found["insptfx"],
        "BD {curifx:w} C9 40 30 03 20 {smalpfx:w} BD {curfx2:w} F0 ?? C9 20 B0 ?? 0A A8 "
        "B9 {bigfxtable:w} 8D {indexjp_ref:w} B9 ?? ?? 8D ?? ?? BD {curval:w} 4C {at_indexjp:w} 60",
    )
    for name, offset in (("setinbl", 0), ("setnibl", 2), ("setinbh", 8), ("setnibh", 10)):
        scan.put("SETINBL", name, at + offset)
    scan.put("SETINBL", "multi3c", at + 24)
    at = scan.at(
        "SETINBL",
        at,
        "B1 {zp} 29 F0 0D {valstor_ref:w} 60 B1 {zp} 29 0F 8D {mergest_ref:w} A9 {at_valstor} "
        "0A 0A 0A 0A 09 {at_mergest} 60 8D {mul3tmp_ref:w} 0A 69 {at_mul3tmp} 60",
    )
    at = scan.at(
        "SMALPFX",
        scan.found["smalpfx"],
        "48 4A 4A 4A 4A A8 B9 {smallfxtbl:w} 8D {indexj2_ref:w} 68 29 0F 8D {valstor_ref:w} "
        "18 90 {at_indexj2}",
    )
    scan.put("SMALPFX", "indexj2_end", at)


# The note effects $78..$7E through NOTEFXTBL ($7F takes its zero offset).
_NOTE_FX = (
    "<portame_notefx: A0 FF A9 6E 20 {setslid:w} 4C {insptfx:w} | BD {curifx:w} C9 40 >",
    "09 02 D0 ??",
    "29 FD 4C ?? ??",
    "09 04 D0 ??",
    "29 FB 9D {wfghost:w} 4C {insptfx:w}",
    "A9 FF 9D {ptngate:w} BD {wfghost:w} 09 01 D0 ??",
    "<gateoffptr: A0 0C B1 {zp} D0 0E |> A9 FE 9D {ptngate:w} 3D {wfghost:w} 9D {wfghost:w} "
    "<gateoffptr: 4C ?? ?? 9D {wftpos:w} A0 0D B1 {zp} F0 03 9D {pwtpos:w} EC {fltctrl_ref:w} "
    "D0 ?? C8 B1 {zp} F0 03 8D {fltposi_ref:w} |>",
)
# The small effects $2x..$Fx through SMALLFXTBL, by high nibble.
_SMALL_FX = {
    2: "<allghostregs: BD {sidg_ad:w} 20 {setnibh:w} | A0 03 20 {setinbh:w} > 4C {writead:w}",
    3: "<allghostregs: BD {sidg_ad:w} 20 {setnibl:w} | A0 03 20 {setinbl:w} > 9D {sidg_ad:w} 60",
    4: "BD {wfghost:w} 20 {setnibh:w} 9D {wfghost:w} 60",
    5: "<allghostregs: BD {sidg_sr:w} 20 {setnibh:w} | A0 04 20 {setinbh:w} > 4C {writesr:w}",
    6: "<allghostregs: BD {sidg_sr:w} 20 {setnibl:w} | A0 04 20 {setinbl:w} > 9D {sidg_sr:w} 60",
    7: "<chordsupport: 9D {curchord:w} A8 B9 {p_chdp4:w} 9D {chordpos:w} |> 60",
    8: "A0 05 20 {setinbh:w} 4C {bigfx08:w}",
    9: "<vibfreqfx_supp: 0A 9D {vibfrequ:w} |> 60",
    0x0A: "8D {mainvol_ref:w} <seq_fx_support: 8D {seqvolu_ref:w} |> 60",
    0x0B: "<filter_smallfx: 0A 0A 0A 0A 8D {fltband_ref:w} |> 60",
    0x0C: "<arpspeedsupp: 9D {arpsped:w} A9 FF 9D {arpscnt:w} |> 60",
    0x0F: "<filter_smallfx: 0A 0A 0A 0A 8D {resonib_ref:w} |> 60",
}
# The big effects $01..$1F through BIGFXTABLE.
_BIG_FX = {
    1: "A0 81 D0 {to_setslid}",
    2: "A0 82 48 98 9D {slidevib:w} 68 4C {setfmod:w}",
    3: "A0 83 D0 {to_setslid}",
    4: "9D {wfghost:w} 60",
    5: "9D {sidg_ad:w} 60",
    6: "9D {sidg_sr:w} 60",
    8: "48 20 {forcvib:w} 68 4C {setvibr:w}",
    9: "20 {multi3c:w} 69 10 9D {wftpos:w} 60",
    0x0A: "20 {multi3c:w} A0 0A 71 {zp} 9D {pwtpos:w} <table_fx_resets: A9 00 9D {pweepcnt:w} |> 60",
    0x0B: "20 {multi3c:w} A0 0B 71 {zp} 8D {fltposi_ref:w} "
    "<table_fx_resets: A9 00 8D {cwepcnt_ref:w} |> 60",
    0x0D: "9D {detuner:w} 60",
    0x0E: "29 0F 9D {pwhigho:w} 60",
    0x0F: "8D {ctfhgho_ref:w} 60",
    0x10: "09 80 8D {p_tmpt6:w} A9 00 <tempoprgsupp: 8D {tmppos:w} 8D ?? ?? 8D ?? ?? "
    "8D {tmpbase:w} 8D ?? ?? 8D ?? ?? |> 60",
    0x11: "<tempoprgsupp: 48 29 0F 09 80 8D {p_tmpt7:w} 68 4A 4A 4A 4A 10 ?? | 60 >",
    0x12: "<tempoprgsupp: F0 ?? A8 B9 {p_tmpp1:w} 4C ?? ?? | 60 >",
    0x13: "<tempoprgsupp: 09 80 BC {trktmpos:w} 99 {p_tmpt8:w} 98 9D {tmpbase:w} "
    "9D {tmppos:w} 60 | 60 >",
    0x14: "<tempoprgsupp: 48 4A 4A 4A 4A 20 ?? ?? 68 29 0F 09 80 99 {p_tmpt9:w} 60 | 60 >",
    0x15: "<tempoprgsupp: F0 ?? A8 B9 {p_tmpp2:w} 4C ?? ?? | 60 >",
    0x16: "29 30 9D {slidevib:w} 60",
    0x1C: "<filtshift_supp: 8D {flshift_ref:w} |> 60",
    0x1D: "<delaysupport: BC {trdelay:w} F0 ?? C8 D0 ?? 9D {trdelay:w} A9 02 9D {spdcnt:w} "
    "DE {trdelay:w} 60 | 60 >",
    0x1E: "<delaysupport: BC {trdelay:w} D0 ?? 18 7D {spdcnt:w} 9D {spdcnt:w} 4C ?? ?? | 60 >",
    0x1F: "<filt_ctrl_fx: 48 29 0F 8D {fswitch_ref:w} 68 29 F0 8D {resonib_ref:w} |> 60",
}


def _effects(scan: _Scan) -> None:
    """Each effect the dispatch tables reach is the routine its number names in ``player.asm``."""
    notes, end1 = scan.found["notefxtbl"] + 0x78, scan.found["indexj1_end"]
    for k, text in enumerate(_NOTE_FX):
        scan.at(f"note effect ${0x78 + k:02x}", end1 + scan.view[notes + k], text)
    if scan.view[notes + 7] != 0:
        raise ValueError("SID-Wizard NOTEFXTBL: $7F does not take the zero offset")
    small, end2 = scan.found["smallfxtbl"], scan.found["indexj2_end"]
    for n, text in _SMALL_FX.items():
        scan.at(f"small effect ${n:x}x", end2 + scan.view[small + n], text)
    detune = end2 + scan.view[small + 0x0D]
    if scan.flags["detune_smallfx"] is not False:
        on = scan.match("small effect $Dx", detune, "0A 0A 0A 4C {setdetu:w}") is not None
        scan.flags["detune_smallfx"] = on
    if not scan.flags["detune_smallfx"] and detune != end2 + scan.view[small + 0x0E]:
        raise ValueError("SID-Wizard SMALFXD is neither the detune effect nor SMALFXE")
    scan.at(
        "small effect $Ex",
        end2 + scan.view[small + 0x0E],
        "<wfctrl_smallfx: BD {wfghost:w} 20 {setnibl:w} <wfctrl_fx_stores: <wfctrl_jmp: "
        "4C {writewf:w} | 9D {wfghost:w} 60 > | 60 > | 60 >",
    )
    big = scan.found["bigfxtable"]
    target = {n: scan.word(big + 2 * n) for n in range(1, 0x20)}
    for n, name in ((2, "setslid"), (4, "writewf"), (5, "writead"), (6, "writesr"), (8, "bigfx08")):
        scan.put("BIGFXTABLE", name, target[n] + (2 if n == 2 else 0))
    scan.put("BIGFXTABLE", "setdetu", target[0x0D])
    scan.put("BIGFXTABLE", "forcvib", target[0x16] - 4)
    scan.at("FORCVIB", scan.found["forcvib"], "A0 00 B1 {zp} 29 30 9D {slidevib:w} 60")
    if target[7] != end2 + scan.view[small + 7] or target[0x0C] != end2 + scan.view[small + 0x0C]:
        raise ValueError("SID-Wizard BIGFXTABLE: $07/$0C are not SMALFX7/SMALFXC")
    if any(target[n] != target[0x1C] for n in range(0x17, 0x1C)):
        raise ValueError("SID-Wizard BIGFXTABLE: $17..$1B are not BIGFX1C")
    for n, text in _BIG_FX.items():
        scan.at(f"big effect ${n:02x}", target[n], text)
    if scan.flags["tempoprgsupp"]:
        scan.at("TRAKTMP", scan.found["traktmp"], "09 80 BC {trktmpos:w} 99 {p_tmpt8:w}")


def _sfx(found: dict) -> None:
    """An SFX build's channel-3 parts: SFX_HR silences voice 3 on the chip itself."""
    sid = found["sid_resfc"] - 0x17
    voice3 = {
        "curins_v2": "curins",
        "curnot_v2": "curnot",
        "curfx2_v2": "curfx2",
        "curifx_v2": "curifx",
    }
    regs = {"sfx_ad": sid + 0x13, "sfx_sr": sid + 0x14, "sfx_wave": sid + 0x12}
    if any(found[k] != found[v] + 14 for k, v in voice3.items()) or any(
        found[k] != v for k, v in regs.items()
    ):
        raise ValueError("SID-Wizard SFX code does not address the third voice")


def _vars(found: dict, flags: dict) -> None:
    """Every per-voice variable laid out from VARIABLES, and CONST_VAR's bunch."""
    present = {
        "tmpbase": any(
            flags[n] for n in ("tempoprgsupp", "arpspeedsupp", "pwkeybtrack", "chordsupport")
        ),
        "sidg": flags["allghostregs"],
    }
    at = found["ptnpos"] - (CHANNELS * VOICE_STRIDE + 3)
    for gate, names in _BUNCHES:
        if gate is not None and not present[gate]:
            continue
        for k, name in enumerate(names):
            if found.setdefault(name, at + k) != at + k:
                raise ValueError(
                    f"SID-Wizard {name} is at ${found[name]:04x}, not VARIABLES' ${at + k:04x}"
                )
        at += CHANNELS * VOICE_STRIDE
    for k, name in enumerate(_CONST_BUNCH):
        if found.setdefault(name, found["flswtbl"] + k) != found["flswtbl"] + k:
            raise ValueError(f"SID-Wizard {name} is not in CONST_VAR's bunch")


def _entries(view: bytes) -> List[Tuple[int, int, int, Optional[int]]]:
    """``(entry, PLAYER, COMMONREGS, MULPLY)`` of each player in ``view``: the entry is
    PLAYER or a hand-edited gate in front of it, MULPLY None in a build without it."""
    flags: Dict[str, Optional[bool]] = {"retainzeropage": None, "sfx": None}
    players, mulplies, end = [], {}, -1
    starts = {m.start() - k for m in re.finditer(rb"\xa2\x0e", view) for k in (0, 6)}
    for addr in sorted(a for a in starts if a >= 0):
        if addr <= end:
            continue
        probe = _Scan(view, dict(flags))
        stop = probe.match("PLAYER", addr, _PLAYER)
        if stop is not None and view[stop] != 0x4C:
            gate = _gate(view, addr)
            players.append((addr if gate is None else gate["entry"], addr, stop))
            end = stop
        elif probe.match("MULPLY", addr, _MULPLY) is not None:
            mulplies.setdefault(probe.found["commonregs"], addr)
            end = probe.found["commonregs"] - 1
    return [(e, p, c, mulplies.get(c)) for e, p, c in players]


def _gate(view: bytes, player: int) -> Optional[dict]:
    """The hand-edited gate in front of ``player``: its fields, or None."""
    entry = player - CodePattern(_GATE).length
    scan = _Scan(view, {})
    try:
        if entry < 0 or scan.match("the play gate", entry, _GATE) is None:
            return None
    except ValueError:
        return None
    if scan.found["gate"] != entry + 1 or scan.found["gate_branch"] not in (0x10, 0x30):
        return None
    return {**scan.found, "entry": entry, "gate_minus": scan.found["gate_branch"] == 0x30}


def entries(view) -> set:
    """Every SID-Wizard play entry (PLAYER or its gate) and MULPLY in ``view``."""
    view = bytes(view)
    return {e for entry, _p, _c, m in _entries(view) for e in (entry, m) if e is not None}


def detect(view, entry: int) -> tuple:
    """``(features, quirks)`` of the player whose PLAYER is ``entry``, read from its code."""
    view = bytes(view)
    players = sorted(p for _e, p, _c, _m in _entries(view))
    load = max(0, entry - 0x200)
    end = next((p - 0x200 for p in players if p > entry), len(view))
    features, quirks, _ = detect_variant(
        view[load:end], load, view, features_for_driver(DRIVER_NORMAL)
    )
    return features, quirks


def _flags(features, quirks) -> Dict[str, Optional[bool]]:
    """The build's flags and release quirks; None for each the binder reads from the code."""
    flags: Dict[str, Optional[bool]] = {
        facet.name: None if facet.detector is None else bool(getattr(features, facet.name))
        for facet in FLAG_FACETS
    }
    flags.update({k: v for k, v in asdict(quirks).items() if isinstance(v, bool)})
    flags.update({name: None for name in _RELEASE})
    flags["subtunejump"] = None
    flags["tablereset"] = bool(flags["pwresetsw"] or flags["filtresetsw"])
    return flags


def _hrender_exit(found: dict, flags: dict) -> None:
    """VIBSLIDEALWAYS / FILTERALWAYS / PULSEALWAYS: the stage HRENDER jumps to."""
    exit_to = {"cntply2": "vibslidealways", "filtprg": "filteralways", "setpwid": "pulsealways"}
    exits = [flag for name, flag in exit_to.items() if found[name] == found["hr_exit"]]
    if not exits and found["hr_exit"] != found["wfarptb"]:
        raise ValueError("SID-Wizard HRENDER exits to none of its stages")
    for flag in exit_to.values():
        flags[flag] = flag in exits


def _scan(scan: _Scan, player: int, commonregs: int, mulply: Optional[int]) -> dict:
    """Every routine from PLAYER (and MULPLY) on, and the play gate in front of PLAYER."""
    scan.put("PLAYER", "player", player)
    scan.at("PLAYER", player, _PLAYER)
    if scan.flags["sfx"]:
        scan.at("SFX_HR", scan.found["sfx_hr"], "8D {sfx_ad:w} 8D {sfx_sr:w} 8D {sfx_wave:w} 60")
    scan.put("PLAYER", "commonregs", commonregs)
    _commonregs(scan, commonregs)
    if mulply is not None:
        _mulply(scan, mulply)
    gate = _gate(scan.view, player) or {"gate": 0, "gate_mask": 0, "gate_minus": False}
    scan.found.update({k: gate[k] for k in ("gate", "gate_mask", "gate_minus")})
    _dotrack(scan)
    _cntplay(scan)
    _subroutines(scan)
    _effects(scan)
    return scan.found


def binding(view, entry: Optional[int] = None, variant: Optional[tuple] = None) -> dict:
    """The playspec layout of the player whose PLAYER or MULPLY is ``entry`` (default: the
    first PLAYER) in the 64 KiB ``view``, for ``variant`` ``(features, quirks)`` (default:
    :func:`detect`'s). Raises :class:`ValueError` for a build the spec does not state.
    """
    view = bytes(view)
    found_entries = [e for e in _entries(view) if entry is None or entry in (e[0], e[3])]
    if not found_entries:
        where = "" if entry is None else f" at ${entry:04x}"
        raise ValueError(
            f"no single-SID SID-Wizard play entry{where} (multi-SID builds are not stated)"
        )
    play_entry, player, commonregs, mulply = found_entries[0]
    features, quirks = variant or detect(view, player)
    flags = _flags(features, quirks)
    found = _scan(_Scan(view, flags), player, commonregs, mulply)
    flags["play_gate"] = play_entry != player and entry in (None, play_entry)
    _hrender_exit(found, flags)
    _vars(found, flags)
    if flags["sfx"]:
        _sfx(found)
    found["sid"] = found["sid_resfc"] - 0x17
    if not flags["allghostregs"]:
        found["sidg"] = found["sid"]
    for name in _OPTIONAL:
        found.setdefault(name, 0)
    del flags["tablereset"]
    found.update(
        {k: bool(getattr(features, k, False)) if v is None else v for k, v in flags.items()}
    )
    found.update(chn=CHANNELS, mulply=entry is not None and entry == mulply, order=list(_ORDER))
    found["chain"] = [a * 16 + b for a, b in zip(_ORDER, _ORDER[1:], strict=False)]
    return found


def labels(view, entry: Optional[int] = None) -> Dict[str, int]:
    """The addresses :func:`binding` finds, by name."""
    found = binding(view, entry)
    return {k: v for k, v in found.items() if isinstance(v, int) and not isinstance(v, bool)}


def bind(view, entry: int) -> dict:
    """:func:`binding` as ``pysidtracker playspec``'s ``--binder`` calls it."""
    return binding(view, entry)


def binder(variant: Optional[tuple] = None) -> Callable[[bytes, int], dict]:
    """A ``bind(view, entry)`` for :class:`~pysidtracker.playspec.SpecPlayer`."""
    return lambda view, entry: binding(view, entry, variant)


def row_reader(view) -> int:
    """The address of the row reader: its ``LDY PTNPOS,X``."""
    return binding(view)["read_row"]


def reader_end(view) -> int:
    """The address after the row reader's last instruction (``STA PTNPOS,X``)."""
    return binding(view)["reader_end"]


def clock(view) -> int:
    """The address of the tick clock: its first instruction."""
    return binding(view)["dotrack"]


def clock_stop(view) -> int:
    """The clock's ``CMP #0``: where both its exits have written all they write."""
    return binding(view)["tick_0"] - 7
