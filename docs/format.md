# SID-Wizard format

## Overview

[SID-Wizard](https://csdb.dk/release/?id=258573) is a C64 music tracker by
Hermit (Mihaly Horvath). `pysidwizard` implements its SWM file format and player
IRQ from first principles, reads and writes native `.swm` modules byte-exactly,
and additionally reads SID-Wizard `.sid` exports (plain or packed `SWP`) into the
same `SWMFile` model for playback.

## Container and detection notes

### SWM module (native)

`read_swm` auto-detects both bare SWM payloads and PRG-wrapped files (2-byte
little-endian load address, then the `SWM1` magic at offset 2). The writer emits
the PRG wrapper by default; set `swm.load_address = None` for a bare payload.

### SID-Wizard `.sid` export (read-only, lossy)

SID-Wizard's editor exports a `.sid` by bundling its 6502 player code with the
tune data, transforming the serialized SWM body into the player's runtime
in-memory layout. This is a **new reader path**, parallel to `parse_swm`, that
understands the runtime layout:

| | `.swm` on disk | `.sid` embedded data |
|---|---|---|
| Container | bare payload or 2-byte PRG wrapper | PSID/RSID header + C64 memory image |
| Tune data | serialized, back-packed, parsed backwards with size bytes | player runtime layout with absolute pointer tables |
| Patterns | `[packed][size][length]` | `[packed][$FF]`, no stored row length |
| Instruments | header + 3 tables + 8-char name | header + 3 tables, name omitted |
| Sequences | `[bytes][delim][len]`, fixed 128-byte stride | bytes in place, reached via subtune pointer table |
| Player code | none | ~2–11 KB of 6502, prepended |

Recovering an `SWMFile` from a `.sid` is **lossy** and does not round-trip back
to the original `.swm`:

- **Instrument names are gone** — dropped by the exporter's compaction step;
  placeholders like `INST 01` are synthesised.
- **Pattern musical length is gone** — re-derived by walking the packed body
  (expanding `$70..$77` NOP runs); the editor's displayed length is not
  recoverable when it differed from the played length.
- **Obsolete header fields** (highlight, color theme, keyboard type, mute,
  config bits) are zeroed/cropped in the export.

Treat `.sid` reading as **read-for-playback**; `build_swm(read_sid(x))` is not
byte-identical to `x`, and `.sid` is not part of the writer's contract.

Both plain and **packed (`SWP`)** exports are decoded. "Packed" is a misnomer —
the tune data is not compressed; it is the same runtime layout with *relative*
pointer tables located via an offset-table in the `SWP1` header. Some packed
exports ship a stale player-template `SWM1` header or omit it, so for `SWP` tunes
the structural counts come from the table extents and the lossy metadata
defaults when no real header is present. No exomizer/cruncher is involved; the
embedded tune data is always stored fully expanded (the only surviving
compression is the player's native NOP-RLE inside patterns, `$70..$77`).

Scope is **single-SID** `PSID`/`RSID` tunes (plain or `SWP`) — the large majority
of SID-Wizard tunes in HVSC. Of the 1126 tunes `sidid` labels
`Hermit/SidWizard_V1.x`, 1058 parse and play; 50 are multi-SID (out of scope) and
18 resolve to no coherent layout (stale/template header, or tune data only
materialised by running the player's init). All raise `SIDFormatError` (a
subclass of `SWMFormatError`).

### Relocation-invariant code-scan reader

The header/end-probe layout above assumes an in-place absolute-pointer layout at
a known end-of-data. Relocated / alternate-layout exports (varied load addresses
`$0800`/`$8000`/`$a000`/`$e000`…) and **magic-less** exports (no `SWM1`/`SWP1` at
all) break that assumption. Both are recovered by reading the table base
addresses **straight from the player-code operands**, which is relocation
invariant: the player relocates as one block, so the absolute addresses baked
into its indexed-load instructions move with it.

Using [`pysidtracker`](https://github.com/anarkiwi/pysidtracker)'s masked 6502
`CodePattern` / `find_code_all` (opcode skeleton fixed, per-tune operand captured
as a little-endian word), each table's access idiom is matched and its operand
read (mapped from the vendored player source `include/player.asm`):

| Table | Player instruction idiom | Operand read |
|---|---|---|
| pattern LO/HI | `ldy CURPTN,x; lda PPTRLO,y; sta zp; lda PPTRHI,y; sta zp` | `ptn_lo`, `ptn_hi` |
| instrument LO/HI | `ldy CURINS,x; lda INSPTLO,y; …; lda INSPTHI,y; …` | `inst_lo`, `inst_hi` |
| subtunes | `adc #imm; tay; lda SUBTUNES,y` | `subtune_base` |
| chord table | `lda CHORDS,y; cmp #$7E` | `chord_base` |
| chord ptr | `tay; lda CHDPTRLO,y; sta CHORDPOS,x` | (upper bound) |
| tempo table | `sec; sbc TEMPOTBL-1,y` | `tempo_base` |
| tempo ptr | `tay; lda TEMPTRLO,y; jmp` | (upper bound) |

The pattern and instrument pointer tables share the same opcode skeleton; they
are told apart by address order (the instrument tables sit below the pattern
tables) and each candidate pairing is validated by decoding. **Counts come from
the table extents** baked into the code (`pat_amount = ptn_hi − ptn_lo`,
`inst_amount = inst_hi − inst_lo − 1`), not the header, so a stale/relocated
header is not trusted. The subtune-table base is taken from `p_subt1` when
present, else located by scanning below `inst_lo`; the sequence count comes from
the header when present, else the geometry (or is searched `8..1`). A
**partially-relocated** export leaves some pointer-table entries at their
original (pre-relocation) address — any entry that does not land inside the
loaded image is repaired with the `load − $1000` delta (SID-Wizard's canonical
assembly origin). A layout is accepted only when every orderlist pattern
reference resolves; otherwise the reader rejects it cleanly.

Magic-less exports are still detected as `DIRECT` (statically, no init) by
anchoring on the SID-Wizard 1.x player-code signature `F0 04 C0 60 90 03 4C ?? ??
BC` — the same fragment `sidid` matches for `Hermit/SidWizard_V1.x`.

### Detection and relocation

The container is unwrapped through the shared
[`pysidtracker`](https://github.com/anarkiwi/pysidtracker) base and header
addresses are not trusted. `is_sidwizard_sid(data)` gates on the PSID/RSID magic
plus the `SWM1`/`SID-WIZARD` signature. The header's load-address field is
`0x0000` in every SID-Wizard export — the real load address is the first 2 bytes
of the data area (LE) at `dataOffset`, so relocation (non-`$1000` players:
`$0FB8`/`$0C00`/`$E000`/`$A000`/… seen in the corpus) is handled by the single
`file_offset = abs_addr − load_address + dataOffset` formula. The `SWM1` 64-byte
tune header, when present (971/1074 corpus tunes), sits at `load+0x20`/`load+0x21`
and carries the counts, driver type, and author; it survives in the static
image.

### Embedded runtime layout

After the optional CIA multispeed starter and the player code, the tune-data
regions run low→high address: **sequences** (orderlist bytes, reached via the
subtune table), **patterns** (`[packed][$FF]`, NOP-packed), **instruments**
(16-byte header + 3 inline `$FF`-tables, no name), **chordtable**, **tempotable**,
then the trailing pointer tables — **subtunes table** (per subtune `CHN_AMOUNT`
LO/HI seq pointers + funktempo pair), **tempo-pointer**, **chord-pointer**,
**instrument LO/HI** and **pattern LO/HI** pointer tables. Pointers in the
pointer-table regions are absolute C64 addresses resolved by the offset formula
above.

The regions are located structurally, version-independently: read the 64-byte
header (`SWM1`) for counts and driver type, then parse the trailing pointer
tables **backwards from the end of data** (pattern-HI/LO, instrument-HI/LO, then
chord-/tempo-pointer tables and the subtunes table) — the same spirit as the
native backward `.swm` parser, avoiding any hardcoded per-version player size.
(The exporter's `PlrEnds[drivertype]` data placement is a per-driver, per-version
constant, kept only as a sanity cross-check, not the primary locator.)

The runtime instrument body is byte-identical in layout to what
`Instrument.decode` parses; patterns reuse `unpack_pattern` + `Pattern.decode`
(length derived from the unpacked row count) and sequences reuse
`decode_sequence`, so the reader's work is locating regions and resolving
pointers, not decoding bytes.

## Data model

An SWM module is a tracker module split into typed pieces. You don't need the
byte layout to use the library, but the moving parts:

**Orderlist (`sequences`).** Three independent sequences, one per SID channel.
Each is a list of typed commands (`PlayPattern`, `Transpose`, `TempoOverride`,
`End`, `Loop`). Channels advance independently; if v0 is playing a long pattern
and v1 finishes early, v1 stays silent until its sequence loops.

**Patterns (`patterns`).** A list of rows; each row may set `note` /
`instrument` / `fx` / `fx_value` columns. The player runs one row every `tempo`
frames (or alternating funktempo left/right values), or whatever the current
`TempoOverride` says.

**Instruments (`instruments`).** A fixed ADSR / vibrato / hard-restart header
plus three per-instrument tables the player walks once per frame while the
instrument is active:

- **Waveform / arp table** (`wf_table`). Three-byte rows `(waveform, arp_pitch,
  detune)`. Drives SID `CTRL` and the per-note pitch offset. Arp byte:
  `$00..$7E` relative pitch up; `$7F` take next pitch from the active chord
  table; `$80` NOP; `$81..$DF` absolute pitch (low 7 bits); `$E0..$FF` relative
  pitch down; `$FE` (waveform column) jump to the row whose offset is the next
  byte; `$FF` end of table.
- **Pulse-width table** (`pw_table`). Three-byte rows: *set* mode (high byte
  `$80..$FD` + low byte → SID PW register) or *sweep* mode (low cycle-count byte,
  signed delta, key-track byte).
- **Filter table** (`filter_table`). Three-byte rows controlling the global SID
  filter while this instrument owns it (one voice at a time): *set* mode encodes
  band switches (LP/BP/HP) + resonance + cutoff hi; *sweep* mode drifts the
  cutoff with a signed delta over a cycle count.

**Chord table (`chord_table`).** A flat byte array indexed by chord number; the
`$7F` arp byte looks up the next chord pitch here. Each table terminates on
`$FF`; jumps use `$FE` + offset. pysidwizard handles serialisation — pass table
*contents* only.

### Sequence command types

| Command                | Meaning                                                     |
| ---------------------- | ----------------------------------------------------------- |
| `PlayPattern(n)`       | Play pattern *n* (1-based; `0` is the reserved empty slot). |
| `Transpose(semitones)` | Per-channel transpose, -16..+15 semitones.                  |
| `TempoOverride(value)` | Switch to row-delay `value` until the next override.        |
| `End()`                | Terminate the sequence without looping.                     |
| `Loop(position)`       | Terminate and jump back to `position` within the sequence.  |
| `RawSequenceByte(b)`   | Opaque single byte preserved for byte-exact round-trip.     |

### Row fields

Every column of a `Row` is `None` by default and contributes nothing.

- `note`: a pitch (`0..0x5F`) or a note-column effect:

  | Code   | Constant         | Meaning                                  |
  |--------|------------------|------------------------------------------|
  | `0x78` | `PORTAMENTO_FX`  | Pre-arm tone portamento for the next row |
  | `0x79` | `SYNC_ON_FX`     | Enable hard sync                         |
  | `0x7A` | `SYNC_OFF_FX`    | Disable hard sync                        |
  | `0x7B` | `RING_ON_FX`     | Enable ring modulation                   |
  | `0x7C` | `RING_OFF_FX`    | Disable ring modulation                  |
  | `0x7D` | `GATE_ON_FX`     | Force gate on                            |
  | `0x7E` | `GATE_OFF_FX`    | Force gate off                           |

- `instrument`: a `1..0x3E` instrument index, or an instrument-column effect —
  value `< 0x40` selects an instrument, `>= 0x40` runs a small effect. The same
  small-effect dispatch is available in the `fx` column (any value `>= 0x20`):

  | Range          | Effect                                                      |
  |----------------|-------------------------------------------------------------|
  | `0x20..0x2F`   | Set attack nibble of post-HR ADSR                           |
  | `0x30..0x3F`   | Set decay nibble of post-HR ADSR; `0x3F` is *legato*        |
  | `0x40..0x4F`   | Set waveform nibble (not yet modelled)                      |
  | `0x50..0x5F`   | Set sustain ("note volume")                                 |
  | `0x60..0x6F`   | Set release                                                 |
  | `0x70..0x7F`   | Set chord (`0x7n` → chord `n`, looked up in `chord_table`)  |

- `fx`: a `1..0xFF` effect code. Values `< 0x20` are *big-FX* and require an
  `fx_value`:

  | Code   | Big-FX                                                                |
  |--------|-----------------------------------------------------------------------|
  | `0x01` | Pitch slide up (value = step per frame)                               |
  | `0x02` | Pitch slide down                                                      |
  | `0x03` | Tone portamento to the row's note (value = slide speed)               |
  | `0x08` | Set vibrato amp + freq                                                |
  | `0x0B` | Jump to filter-table row (value = row index)                          |
  | `0x10` | Override row delay (= live tempo change)                              |

  Values `>= 0x20` are small-FX, same as the table above.

## Player and playback notes

`SWMPlayer(swm).play_frame()` returns the SID register writes the real
SID-Wizard player would emit on the C64 this frame, in the same order (channel
3 → 2 → 1, then global filter + volume). `iter_writes` yields `(frame, reg,
value)` and deduplicates consecutive identical writes to the same register by
default (matching the schema `sidwizard-driver` produces for ground-truth
captures). `render_wav` drives [`pyresidfp`](https://pypi.org/project/pyresidfp/).

### Player correctness

The four reference tunes in `tests/fixtures/` (flashitback, bronkosaurus,
euphoria, rain8580) collectively exercise the full 1-SID feature surface: pitched
notes, gate-off note-FX, chord tables, multispeed (`frame_speed=2`), funktempo,
BIGFX portamento, vibrato, filter walking, instrument inheritance across F1, the
WRPITCH detune-with-carry chain. For each tune, every frame's full SID-register
state agrees byte-for-byte with the reference captured from real SID-Wizard
running inside [`asid-vice`](https://github.com/anarkiwi/asid-vice) via
[`sidwizard-driver`](https://github.com/anarkiwi/sidwizard-driver). The
integration suite re-derives a fresh reference from the real SID-Wizard binary on
every PR.

### Out of scope

Multi-SID, SFX, slowdown, and non-440 Hz tuning tables.

## References

- [SID-Wizard](https://csdb.dk/release/?id=258573) (Hermit / Mihaly Horvath).
- [`asid-vice`](https://github.com/anarkiwi/asid-vice) /
  [`sidwizard-driver`](https://github.com/anarkiwi/sidwizard-driver) — the
  bit-exact capture oracle.
- [pyresidfp](https://pypi.org/project/pyresidfp/) — reSIDfp SID emulation.
- `hvsc-tracker-catalog` — HVSC `Hermit/SidWizard_V1.x` corpus identification.
- [`pysidtracker`](https://github.com/anarkiwi/pysidtracker) — shared
  container/image/detection base.
