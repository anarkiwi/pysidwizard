# The SID-Wizard playspec

`src/pysidwizard/specs/sidwizard.play` states the SID-Wizard play call (`player.asm`) in
[pysidtracker's playspec DSL](https://github.com/anarkiwi/pysidtracker/blob/main/docs/playspec.md);
`pysidwizard.playspec` binds it to a player in memory.

## What it states

The spec is written from the format, in the playspec's own abstractions, not routine by routine
from `player.asm`:

| Format | Spec |
|---|---|
| PLAYER: the voices last first, then COMMONREGS | `schedule`, `voice[x]`, `commonregs` |
| A tick counter against a tempo program | the `beat` counter and the `tempo_program` handler; `step` dispatches on `ticks` |
| A pattern row: note, instrument, effect, value, runs of empty rows | the `row_reader` cursor, the `row_prep` handler |
| The orderlist: patterns, track end, jumps, subtunes, transpose, volume and tempo effects | `next_pattern`, `order_effect` |
| Hard restart, note start, legato and tone portamento | `hard_restart`, `note_start`, `start_sound` |
| The instrument's pulse, filter and waveform tables | the `pw_table`, `filter_table` and `wave_table` cursors, the `filter_owner` |
| Vibrato, slides, portamento, chords, detune | `tables`, `portamento`, `vibrato`, `wave_row`, `set_vibrato` |
| Note, small and big effects | `note_effect`, `small_effect`, `big_effect` |
| MULPLY, the multispeed entry | `L.mulply`, `multispeed` |
| SFX builds, SWP exports, echo builds, hand-edited play gates | `L.sfx`, `L.swp`, `L.echo_support`, `L.play_gate` |
| 1.0's subtune jump restarting every voice | the `subtune` machine, `restart` |

Every player variable is a `state`, at no address, that starts from what the init left in it
(`L.init_<name>`, a list over the voices for a voice's own). A build's features and the release
differences of the 1.0..1.9 players are flags (`L.<flag>`; the features are those of
`pysidwizard.features.FACETS`) that guard branches, match arms and `ite`. The instrument any voice
last selected is the pointer state `ins`, which a call that keeps the zero page resets to the
init's. The carry WFARPTB's detune add takes in (left by the hard restart, the pulse width or the
waveform row) is the per-voice signal `detune_carry`; the multispeed call's chain from voice to
voice is the `mulcarry` state; the accumulator and zero flag a routine passes
on are parameters. A build with ghost registers writes the chip from the voices' registers once
all have played.

FREQTBL and FREQTBH are one `u16` table, `notefreq @ (L.freqtbl, L.freqtbh)`, declared
`notes(word, -1)`: table index `i` is standard note `i - 1` (index 0 is B-1). The slide and
vibrato steps (VIB_LO, VIB_HI) are the word table `slidestep`, and a subtune's orderlist
pointers the word tables `subtune_order` and `subtune_orders`. `FORMAT.spec_player(swm, notes=clock)` answers every read of
them, past note 95 too, with `pysidtracker.notes.frequency(i - 1, clock)`; native mode
reads the image.

Song data stays addressed by pointer where the language reads it only from memory: `ins` is
the address of the instrument last selected (its three table cursors take a memory base, and an
array table has no base to add to a cursor's offsets), the pattern reader's stream base is
`ptnptr[curptn] + L.swp_base` (a stream cannot read an array), and `seqt` is the orderlist
pointer relative to `L.swp_base`, which the orderlist table's `offset` adds.

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
- `init_<name>`: what the init left in each player variable, read at the addresses its
  routines name, zero for a variable the build lacks. `playspec.binding` returns only the
  names the spec declares; `playspec.labels` every address the binder found.

`binding` raises `ValueError` for players the spec does not state: multi-SID builds,
players partly relocated within a routine (#121), players with effect routines stripped
(#122), slowdown builds (#123), and the early-release and hand-patched builds of #124.
Partly relocated players were moved by a tracing relocator (sidreloc), which
relocates the operands of the instructions it ran and leaves every other one at the
build it relocated from (base `$1000` or `$4000`, zero page `$FE`): code the song never
runs (MULPLY, an orderlist `$FE`, unused effects, a tempo-program store) addresses that
build's cells, or jumps into it.

## Verification (`tests/test_playspec.py`)

- `SpecPlayer` over the spec against the tune's own 6502 (`EmuPlayer`): the SID registers,
  with the bits the chip ignores masked, and per voice whether the gate was cleared and set
  again or TEST was set within the call, equal after every call of the whole song, on every
  single-SID 1.97 example export (default suite) and every stated oracle tune (`-m oracle`). A
  tune without a play address (the echo export, whose engine calls PLAYER from its own
  interrupt) has its PLAYER called each frame. `scripts/sw_spec_diff.py` reports the first
  differing call of a tune, `scripts/sw_spec_census.py` runs many in parallel.
- The spec run natively and in Python, call by call, over each export: the SID writes and
  registers equal.
- `pysidtracker playspec check` on a bound layout.
- `scripts/sw_builds.py` groups tunes by the build flags the binder reads;
  `scripts/sw_state_diff.py` finds the first call whose player variables differ between two
  specs; `scripts/sw_native_check.py` lists the tunes a spec does not play on the native core.
