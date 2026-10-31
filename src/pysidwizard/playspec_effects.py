"""The SID-Wizard playroutine's effect routines, as :mod:`pysidwizard.playspec` binds them.

Each effect the note, small and big effect dispatch tables reach is matched as the routine
its number names in ``player.asm``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .playspec import _Scan


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
    8: "<vibamfx_amp_only: 0A 0A 0A 0A 20 {setvamp:w} A0 00 B1 {zp} 29 30 9D {slidevib:w} 60 "
    "| A0 05 20 {setinbh:w} 4C {bigfx08:w} >",
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
    4: "<bigfx_v10: C9 F0 B0 03 4C {writewf:w} 29 0F 20 {multi3c:w} 69 10 9D {wftpos:w} 60 "
    "| 9D {wfghost:w} 60 >",
    5: "9D {sidg_ad:w} 60",
    6: "9D {sidg_sr:w} 60",
    8: "48 20 {forcvib:w} 68 4C {setvibr:w}",
    9: "<bigfx_v10: 29 30 9D {slidevib:w} | 20 {multi3c:w} 69 10 9D {wftpos:w} > 60",
    0x0A: "20 {multi3c:w} A0 0A 71 {zp} 9D {pwtpos:w} "
    "<table_fx_resets: A9 00 9D {pweepcnt:w} |> 60",
    0x0B: "20 {multi3c:w} A0 0B 71 {zp} 8D {fltposi_ref:w} "
    "<table_fx_resets: A9 00 8D {cwepcnt_ref:w} |> 60",
    0x0D: "9D {detuner:w} <bigfx0d_falls: 29 0F 9D {pwhigho:w} 60 | 60 >",
    0x0E: "29 0F 9D {pwhigho:w} 60",
    0x0F: "<bigfx_v10: 8D {flshift_ref:w} | 8D {ctfhgho_ref:w} > 60",
    0x10: "09 80 8D {p_tmpt6:w} A9 00 <tempoprgsupp: 8D {tmppos:w} 8D ?? ?? 8D ?? ?? "
    "8D {tmpbase:w} 8D ?? ?? 8D ?? ?? |> 60",
    0x11: "<tempoprgsupp: 48 29 0F 09 80 8D {p_tmpt7:w} 68 4A 4A 4A 4A 10 ?? | 60 >",
    0x12: "<tempoprgsupp: F0 ?? A8 B9 {p_tmpp1:w} 4C ?? ?? | 60 >",
    0x13: "<tempoprgsupp: 09 80 BC {trktmpos:w} 99 {p_tmpt8:w} 98 9D {tmpbase:w} "
    "9D {tmppos:w} 60 | 60 >",
    0x14: "<tempoprgsupp: 48 4A 4A 4A 4A 20 ?? ?? 68 29 0F 09 80 99 {p_tmpt9:w} 60 | 60 >",
    0x15: "<tempoprgsupp: F0 ?? A8 B9 {p_tmpp2:w} 4C ?? ?? | 60 >",
    0x16: "<bigfx_v10: | 29 30 9D {slidevib:w} > 60",
    0x1C: "<bigfx_v10: | <filtshift_supp: 8D {flshift_ref:w} |> > 60",
    0x1D: "<delaysupport: BC {trdelay:w} F0 ?? C8 D0 ?? 9D {trdelay:w} A9 02 9D {spdcnt:w} "
    "DE {trdelay:w} 60 | 60 >",
    0x1E: "<delaysupport: BC {trdelay:w} D0 ?? 18 7D {spdcnt:w} 9D {spdcnt:w} 4C ?? ?? | 60 >",
    0x1F: "<filt_ctrl_fx: 48 29 0F 8D {fswitch_ref:w} 68 29 F0 8D {resonib_ref:w} |> "
    "<bigfx1f_toggle: AD {fswitch_ref:w} 49 08 8D {fswitch_ref:w} |> 60",
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
    _bigfx(scan, end2 + scan.view[small + 7], end2 + scan.view[small + 0x0C])


def _bigfx(scan: _Scan, smalfx7: int, smalfxc: int) -> None:
    """BIGFXTABLE's routines: the effects, and the labels its entries name."""
    big = scan.found["bigfxtable"]
    target = {n: scan.word(big + 2 * n) for n in range(1, 0x20)}
    scan.at("big effect $04", target[4], _BIG_FX[4])
    v10 = scan.flags["bigfx_v10"]
    for n, name in ((2, "setslid"), (4, "writewf"), (5, "writead"), (6, "writesr"), (8, "bigfx08")):
        if n != 4 or not v10:
            scan.put("BIGFXTABLE", name, target[n] + (2 if n == 2 else 0))
    scan.put("BIGFXTABLE", "setdetu", target[0x0D])
    if scan.flags["bigfx_delay_16"] is None:
        scan.flags["bigfx_delay_16"] = target[0x16] == target[0x1D] != target[0x1E]
    delay16 = scan.flags["bigfx_delay_16"]
    scan.put("BIGFXTABLE", "forcvib", target[9 if v10 or delay16 else 0x16] - 4)
    scan.at("FORCVIB", scan.found["forcvib"], "A0 00 B1 {zp} 29 30 9D {slidevib:w} 60")
    if target[7] != smalfx7 or target[0x0C] != smalfxc:
        raise ValueError("SID-Wizard BIGFXTABLE: $07/$0C are not SMALFX7/SMALFXC")
    if any(target[n] != target[0x1C] for n in range(0x17, 0x1C)):
        raise ValueError("SID-Wizard BIGFXTABLE: $17..$1B are not BIGFX1C")
    for n, text in _BIG_FX.items():
        if delay16 and n in (0x16, 0x1C):
            if target[n] != target[0x1D]:
                raise ValueError(f"SID-Wizard BIGFXTABLE: ${n:02x} is not BIGFX1D")
            continue
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
