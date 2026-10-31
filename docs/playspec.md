# The SID-Wizard playspec

`src/pysidwizard/specs/sidwizard.play` states the SID-Wizard play call (`player.asm`) in
[pysidtracker's playspec DSL](https://github.com/anarkiwi/pysidtracker/blob/main/docs/playspec.md);
`pysidwizard.playspec` binds it to a player in memory.

## What it states

| `player.asm` | Spec |
|---|---|
| PLAYER: DOTRACK per voice, the last voice first | `schedule`, `voice[x]` (`x` 0..2, X = 7·x) |
| DOTRACK's tick clock and the TICK_0 / TICK_1 / TICK_2 / CNTPLAY dispatch | `dotrack`, `tick` |
| TICK_0: pattern pointer, delayed volume and track tempo, the row | `tick_0`, `read_row` |
| HARDRST / NEWNOTE / HRGTOFF / HRENDER | `hardrst`, `hrender` |
| TICK_1 / PTN_SEQ / SEQSUB / SEQ_FX / SETSEQA | `tick_1`, `ptn_seq`, `seq_step`, `seq_fx`, `setseqa` |
| TICK_2 / SEL_INS / CHKNOTE / STRTINS / CLEGATO / LEGATOO | `tick_2`, `clegato`, `legatoo` |
| STRTSND / SETFLTP / SETADSR | `strtsnd`, `startsound`, `setfltp` |
| CNTPLAY / VIBSLIDE (vibrato, slides, portamento) | `cntplay`, `cntply1`, `vibslide`, `portame`, `dovibra` |
| FilterProgram, SETPWID, WFARPTB (chords) to WRWFGHO | `filtprg`, `setpwid`, `wfarptb`, `wftable` |
| SETVIB0 / SETVIB1 / SETVIBR / SETFMOD / FORCVIB | same names |
| NOTE_FX, INSPTFX, SMALPFX, BIGPTFX and every effect | `note_fx`, `insptfx`, `smalpfx`, `bigfx`, ... |
| COMMONREGS, RETPLAY | `commonregs`, `retplay` |
| MULPLY / MULCNTP | `voice[x]` with `L.mulply`, `mulcntp` |
| SFX builds (channel 3 sound effects) | `L.sfx` |
| SWP exports (relative song pointers) | `L.swp`, `point_lo` / `point_hi` |
| ECHO_SUPPORT builds (the echo engine writes `$D418`) | `L.echo_support` |
| 1.0's SUBTJMP / SETSTUNE / INIT: a subtune jump restarts every voice | `subtune`, `setstune`, a second pass of `voice[x]` (`restart`) |

Each `.if feature.X_ON` is `if L.x` (the names of `pysidwizard.features.FACETS`); each
release difference of the 1.0..1.9 players is a flag too (`first_frame_*`,
`filter_stop_rewinds`, `kbtrack_fswitch`, `wfctrl_fx_stores`, `hr_ad_first`,
`adsr_ad_first`, `seq_tempo_capped`, `table_fx_resets`, and 1.0's `subtune_restarts`,
`sound_tables_first`, `sound_resets_flshift`, `vibamfx_amp_only`, `bigfx_v10`, and the
early releases' `hrender_inline`, `hrgtoff_or8`, `restart_trdelay`, `noins_rts`,
`bigfx0d_falls`, `bigfx_delay_16`, `bigfx1f_toggle`). Self-modified immediates are
`operand` cells; the computed effect dispatch (`bcc *+2` / `jmp abs` from NOTEFXTBL,
SMALLFXTBL, BIGFXTABLE) writes its operand and branches on the effect number. Registers
a routine passes on (the carry into WFARPTB and WRPITCH, the accumulator into WFARPTB,
the zero flag into SETFMOD and the tempo effects) are parameters; the carry one voice's
multispeed call leaves for the next is a signal.

FREQTBL and FREQTBH are `notes(lo, -1)` / `notes(hi, -1)`: table index `i` is standard
note `i - 1` (index 0 is B-1). `FORMAT.spec_player(swm, notes=clock)` answers every read of
them, past note 95 too, with `pysidtracker.notes.frequency(i - 1, clock)`; native mode
reads the image.

## Binding

`playspec.binding(view, entry)` reads everything from the player's code:

- PLAYER and MULPLY by their DOTRACK / MULCNTP call sequences (a hand-edited gate in
  front of PLAYER becomes `L.play_gate`); `playspec.entries(view)` lists them.
- The build flags from `features.detect_variant`; a flag no facet site reads, each
  release difference, `echo_support`, and the flags whose probes misread stripped builds
  (`allghostregs`, `retainzeropage`, `filtkbtrack`, `finefiltsweep`, `hardrestypes`,
  `transposesupp`, `octaveshift`, TABLRST's presence) are the ones whose code the
  routine holds. Code-only shapes (`ghostloop_bpl`, `subtjmp_var`, `row_reader_jmp`,
  `fsb_bmi`, `ptnseq_jmp`, `legato_jmp`) only steer the match.
- Each routine is matched where the control flow from PLAYER reaches it, its pattern
  assembled from the flags as `player.asm` assembles the routine, so a wrong flag fails
  the match. The dispatch tables are checked entry by entry against the routines their
  effect numbers name.
- Per-voice variables are laid out from VARIABLES (the row reader's PTNPOS) in bunches
  of 7 bytes × 3 voices, the TMPBASE and SIDG bunches present as the flags say, in 1.0's
  bunch order or the later one, whichever every matched operand agrees with.
- A variable cell is `per_proc`: a routine that addresses it apart from the rest binds
  `<name>@<proc>` for its spec procs, so a player relocated per routine is stated.

`binding` raises `ValueError` for players the spec does not state: multi-SID builds,
players partly relocated within a routine (#121), players with effect routines stripped
(#122), slowdown builds (#123), and the early-release and hand-patched builds of #124.
Partly relocated players were moved by a tracing relocator (sidreloc), which
relocates the operands of the instructions it ran and leaves every other one at the
build it relocated from (base `$1000` or `$4000`, zero page `$FE`): code the song never
runs (MULPLY, an orderlist `$FE`, unused effects, a tempo-program store) addresses that
build's cells, or jumps into it.

## Verification (`tests/test_playspec.py`)

- `SpecPlayer` over the spec against the tune's own 6502 (`EmuPlayer`): the SID registers
  and the 64 KiB outside the stack page equal after every call of the whole song, on
  every single-SID 1.97 example export (default suite) and every stated oracle tune
  (`-m oracle`). A tune without a play address (the echo export, whose engine calls
  PLAYER from its own interrupt) has its PLAYER called each frame. `scripts/sw_spec_diff.py` reports the first differing call of a tune,
  `scripts/sw_spec_census.py` runs many in parallel.
- `tv.validate` proofs, X = 0: on a set covering every build and, in the oracle job, a
  1.0 player (`pantheon`, with its voice restart); each routine from its label (a stretch to the next
  routine or to a dispatch where its code runs on), each dispatched effect from its
  table entry with its effect number assumed in the column; the PW table by the four
  kinds of its first byte. WFARPTB (to RTS) is proven on builds without chords: a
  chord's `$7E` return re-reads the table up to 256 times, a loop the SMT lowering
  unrolls to its bound.
- `pysidtracker playspec check` on a bound layout.
