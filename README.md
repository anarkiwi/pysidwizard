# pysidwizard

Pure-Python reader, writer, and **bit-exact player** for SID-Wizard
SWM modules. No native dependencies.

[SID-Wizard](https://csdb.dk/release/?id=258573) is a Commodore 64
music tracker by Hermit (Mihaly Horvath). pysidwizard implements its
SWM file format and player IRQ from first principles.

The player is verified against a live capture of SID-Wizard running
inside `asid-vice`: every frame × every SID register, on every PR.
See [Player correctness](#player-correctness) below.

## Install

```bash
pip install pysidwizard
```

## Read an SWM

```python
from pysidwizard import read_swm

swm = read_swm("tune.swm")
print(swm.author_str(), swm.frame_speed, swm.highlight)

# Pattern rows are typed; no byte poking required.
for row in swm.patterns[0].rows[:4]:
    print(row)

# Sequences (one per SID channel) are lists of typed commands.
for cmd in swm.sequences[0][:6]:
    print(cmd)

# Instruments expose fixed fields by name and tables as bytes.
ins = swm.instruments[0]
print(ins.name_str(), ins.attack, ins.decay, ins.sustain, ins.release)
print(ins.wf_table.hex(), ins.pw_table.hex(), ins.filter_table.hex())
```

`read_swm` auto-detects both bare SWM payloads and PRG-wrapped files
(2-byte little-endian load address, then the `SWM1` magic at offset
2). The writer emits the PRG wrapper by default; set
`swm.load_address = None` for a bare payload.

## Read a SID file

SID-Wizard tunes exported to `.sid` (PSID) can be read straight into the
same `SWMFile` model, so the player below works on them unchanged:

```python
from pysidwizard import read_sid, parse_sid, is_sidwizard_sid, SWMPlayer

swm = read_sid("tune.sid")            # or parse_sid(open("tune.sid", "rb").read())
print(swm.author_str(), len(swm.patterns), len(swm.instruments))

player = SWMPlayer(swm)
for _ in range(500):
    player.play_frame()

# Gate it cheaply before parsing a directory of mixed .sid files:
data = open("maybe.sid", "rb").read()
if is_sidwizard_sid(data):
    swm = parse_sid(data)
```

This path is **read-only and lossy**: the exporter discards instrument
names (placeholders like `INST 01` are synthesised) and the editor's exact
per-pattern row length, so do not route the result back through the writer
expecting a byte-exact round-trip.

Scope is **single-SID PSID** files — roughly 95% of the SID-Wizard tunes in
HVSC. `RSID`, multi-SID, and a small tail of files exported by unusual
player variants raise `SIDFormatError` (a subclass of `SWMFormatError`) and
are left for a later phase.

## Build an SWM from scratch

```python
from pysidwizard import (
    End,
    Instrument,
    Pattern,
    PlayPattern,
    Row,
    SWMFile,
    Waveform,
    build_swm,
    straight_tempo,
    write_swm,
)
from pysidwizard.constants import GATE_OFF_FX, SWM_C5_NOTE

instrument = Instrument(
    name=b"LEAD    ",
    control=0x1A,                 # hard-restart timer on, tied PW/CTF off
    hr_attack=0, hr_decay=0xF,
    hr_sustain=0xF, hr_release=0,
    attack=0, decay=0,
    sustain=0xF, release=0,
    default_chord=1,
    first_waveform=Waveform.PULSE,
)

pattern = Pattern(
    rows=[
        Row(note=SWM_C5_NOTE, instrument=1),
        Row(), Row(), Row(), Row(), Row(), Row(),
        Row(note=GATE_OFF_FX),
    ],
    length=8,
)

swm = SWMFile(
    author=b"PYSIDWIZARD DEMO",
    default_pattern_length=8,
    sequences=[[PlayPattern(1), End()]] * 3,
    patterns=[pattern],
    instruments=[instrument],
    subtune_tempos=[(straight_tempo(6), straight_tempo(3))],
)

write_swm(swm, "demo.swm")               # ready to load in SID-Wizard 1.x
assert build_swm(read_swm("demo.swm")) == build_swm(swm)  # byte-exact roundtrip
```

The writer rejects modules that reference patterns or instruments
that don't exist, or sequences missing an `End()` / `Loop()`
terminator. NOP packing, table terminators, and on-disk pointer
arithmetic are handled automatically — you describe content, not
bytes.

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

- `instrument`: a `1..0x3E` instrument index, or an
  instrument-column effect — value `< 0x40` selects an instrument,
  `>= 0x40` runs a small effect. The same small-effect dispatch is
  available in the `fx` column (any value `>= 0x20`):

  | Range          | Effect                                                      |
  |----------------|-------------------------------------------------------------|
  | `0x20..0x2F`   | Set attack nibble of post-HR ADSR                           |
  | `0x30..0x3F`   | Set decay nibble of post-HR ADSR; `0x3F` is *legato*        |
  | `0x40..0x4F`   | Set waveform nibble (not yet modelled)                      |
  | `0x50..0x5F`   | Set sustain ("note volume")                                 |
  | `0x60..0x6F`   | Set release                                                 |
  | `0x70..0x7F`   | Set chord (`0x7n` → chord `n`, looked up in `chord_table`)  |

- `fx`: a `1..0xFF` effect code. Values `< 0x20` are *big-FX* and
  require an `fx_value`:

  | Code   | Big-FX                                                                |
  |--------|-----------------------------------------------------------------------|
  | `0x01` | Pitch slide up (value = step per frame)                               |
  | `0x02` | Pitch slide down                                                      |
  | `0x03` | Tone portamento to the row's note (value = slide speed)               |
  | `0x08` | Set vibrato amp + freq                                                |
  | `0x0B` | Jump to filter-table row (value = row index)                          |
  | `0x10` | Override row delay (= live tempo change)                              |

  Values `>= 0x20` are small-FX, same as the table above.

## Play an SWM

```python
from pysidwizard import SWMPlayer, read_swm

swm = read_swm("tune.swm")
player = SWMPlayer(swm)

# Each play_frame() returns the SID register writes the real
# SID-Wizard player would emit on the C64 this frame, in the same
# order (channel 3 -> 2 -> 1, then global filter + volume).
for _ in range(100):
    for reg, value in player.play_frame():
        print(f"${reg:04X} = ${value & 0xFF:02X}")
```

### Render to WAV

```python
from pysidwizard import render_wav

render_wav("tune.swm", "tune.wav", seconds=60.0, model_name="MOS8580")
```

Or from the command line:

```bash
python -m pysidwizard.player tune.swm tune.wav --seconds 60 --model MOS8580
```

`render_wav` drives [`pyresidfp`](https://pypi.org/project/pyresidfp/)
under the hood (install with `pip install pyresidfp`).

### Iterate raw writes

For SID-state inspection or feeding a different SID emulator:

```python
from pysidwizard import iter_writes

for frame, reg, value in iter_writes(swm, n_frames=1500):
    ...
```

`iter_writes` deduplicates consecutive identical writes to the same
register by default — matching the schema `sidwizard-driver` produces
for ground-truth captures.

## SWM at a glance

An SWM module is a tracker module split into typed pieces. You don't
need to know the byte layout to use this library, but a high-level
picture of the moving parts is useful when reading or writing
patterns:

**Orderlist (`sequences`).** Three independent sequences, one per
SID channel. Each sequence is a list of typed commands
(`PlayPattern`, `Transpose`, `TempoOverride`, `End`, `Loop`).
Channels advance independently; if v0 is playing a long pattern and
v1 finishes early, v1 just stays silent until its sequence loops.

**Patterns (`patterns`).** A list of rows; each row may set
`note` / `instrument` / `fx` / `fx_value` columns. The player runs
one row every `tempo` frames (or alternating funktempo left/right
values), or whatever the current `TempoOverride` says.

**Instruments (`instruments`).** Each instrument is a fixed
ADSR / vibrato / hard-restart header plus three per-instrument
"tables" that the player walks once per frame while the instrument
is active:

- **Waveform / arp table** (`wf_table`). Three-byte rows
  `(waveform, arp_pitch, detune)`. Drives the SID `CTRL` register
  and the per-note pitch offset. Arp byte semantics:
  - `$00..$7E`: relative pitch *up* — add this many semitones to
    the note.
  - `$7F`: take the next pitch from the active chord table.
  - `$80`: NOP — keep the previous pitch.
  - `$81..$DF`: absolute pitch — the low 7 bits become the
    discrete pitch.
  - `$E0..$FF`: relative pitch *down*.
  - `$FE` (in the waveform column): jump to the row whose offset
    is in the next byte.
  - `$FF`: end of table; the walker freezes here.
- **Pulse-width table** (`pw_table`). Three-byte rows. Either
  *set* mode (high byte `$80..$FD`, plus a low byte -> SID pulse-
  width register), or *sweep* mode (low cycle-count byte, signed
  delta, key-track byte).
- **Filter table** (`filter_table`). Three-byte rows controlling
  the global SID filter while this instrument owns it (only one
  voice at a time controls the filter). *Set* mode encodes the
  band switches (LP/BP/HP) + resonance + cutoff hi; *sweep* mode
  drifts the cutoff with a signed delta over a cycle count.

**Chord table (`chord_table`).** A flat byte array indexed by
*chord number*. When the waveform table's arp byte is `$7F`, the
next chord pitch is looked up here. Chords loop until the
instrument's waveform table moves past the chord-trigger row.

Each table terminates on `$FF`. Jumps inside a table use `$FE`
followed by an offset. pysidwizard handles all of this for you
when serialising — pass the table *contents* only.

## Player correctness

The four reference tunes in `tests/fixtures/` (flashitback,
bronkosaurus, euphoria, rain8580) collectively exercise the full
1-SID feature surface: pitched notes, gate-off note-FX, chord
tables, multispeed (`frame_speed=2`), funktempo, BIGFX portamento,
vibrato, filter walking, instrument inheritance across F1, the
WRPITCH detune-with-carry chain.

For each tune, every frame's full SID-register state agrees
byte-for-byte with the reference captured from real SID-Wizard
running inside [`asid-vice`](https://github.com/anarkiwi/asid-vice)
via [`sidwizard-driver`](https://github.com/anarkiwi/sidwizard-driver).
`tests/test_player_reference.py` runs that comparison on every
push.

The integration suite in `tests/integration/` goes one step
further: every PR re-derives a fresh reference CSV from the real
SID-Wizard binary and asserts pysidwizard still matches. If the
binary changes, the integration suite fails before merge.

Out of scope: multi-SID, SFX, slowdown, and non-440 Hz tuning
tables.

## Tests

Unit tests (fast, no Docker):

```bash
pip install -e ".[dev]"
python -m pytest
```

Integration tests (slow, requires Docker; pulls
`anarkiwi/headlessvice`):

```bash
pip install -e ".[integration]"
python -m pytest -m integration tests/integration/
```

The four SWM test tunes are **not** tracked in this repo — they're
SID-Wizard binary artifacts. `tests/_swm_cache.py` fetches them on
demand from the SID-Wizard 1.94 source tarball via
`sidwizard-driver`'s cache and SHA-256 verifies each one.

## Continuous integration

| Workflow                             | What it runs                                                              |
| ------------------------------------ | ------------------------------------------------------------------------- |
| `.github/workflows/test.yml`         | `ruff` + `black` + `pytest` (matrix: Python 3.10-3.13 on Linux + 3.12 on macOS/Windows) |
| `.github/workflows/integration.yml`  | Fresh capture from real SID-Wizard inside `asid-vice`; asserts pysidwizard matches |
| `.github/workflows/publish.yml`      | Builds + uploads to PyPI via trusted publishing on release                |

The integration workflow runs on every push, every PR, and weekly
on cron — so SID-Wizard binary / `sidwizard-driver` / `headlessvice`
drift is caught even when no PR has landed.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
