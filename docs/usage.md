# Usage

Reading `.swm` / `.sid` into an `SWMFile` model is covered in the
[README](../README.md). This document covers the rest of the API. For the format
and data model, see [format.md](format.md).

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

## Read a SID file

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

## Read a standalone SWP file

A bare `SWP1` packed tune (with or without a two-byte load-address prefix) reads
into the same model via `read_swp` / `parse_swp`:

```python
from pysidwizard import read_swp, parse_swp, SWMPlayer

swm = read_swp("tune.swp")             # or parse_swp(open("tune.swp", "rb").read())
player = SWMPlayer(swm)
for _ in range(500):
    player.play_frame()
```

This path is read-only and lossy (see [format.md](format.md#sid-wizard-sid-export-read-only-lossy)).

A `.sid` also carries its player image, so `parse_sid` captures
`SWMFile.player.freq_table_tail`: the bytes the assembler placed after the player's
96-note `FREQTBL`. SID-Wizard's arp pitch lookup *masks* its note index
(`and #$7F`, `player.asm:2525`) rather than clamping it, so a relative arp step
or transpose summing past 95 reads those bytes and one summing past 127 wraps
back into the table. A module built in memory (an `SWMFile` with no image) keeps
that fallback. A module read from a bare `.swm`/`.swp` via `parse_swm` / `read_swm`
(or `parse_swp` / `read_swp`) now defaults to the latest release's player image
for its driver, so it renders as that editor's `.sid` export: pass
`player_version=None` to keep the bare fallback, or name a release (e.g. `"1.7"`).

A `.sid` header song is only the accumulator the tune's init receives. Wrappers
remap it to another subtune, patch the tune data, or call one of several
embedded players (each with its own module). `parse_sid_song(data, song)` runs
the init in the 6502 emulator up to the SID-Wizard player it enters and returns
the single-subtune module that song actually plays, along with
`SWMFile.player.sid_init` (the SID registers the init wrote) and the image's own
`FREQTBL`/`FREQTBH` (`player.freq_lo` / `player.freq_hi`, NTSC exports included):

```python
from pysidwizard import SWMPlayer, parse_sid_song

grid = SWMPlayer(parse_sid_song(open("tune.sid", "rb").read(), 2)).render_grid(500)
```

## Build an SWM from scratch

```python
from pysidwizard import (
    End, Instrument, Pattern, PlayPattern, Row, SWMFile, Waveform,
    build_swm, straight_tempo, write_swm,
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

The writer rejects modules referencing patterns/instruments that don't exist, or
sequences missing an `End()` / `Loop()` terminator. NOP packing, table
terminators, and on-disk pointer arithmetic are handled automatically — describe
content, not bytes.

## Export an SWM to a SID

`pysidwizard.sidmaker` runs SID-Wizard's own exporter (SID-Maker 1.7, 1.8, 1.94 or
1.97, shipped as package data) on the 6502 and returns the `.sid` it saves: PAL,
default relocation, the module's own driver type and subtunes.

```python
from pysidwizard import sidmaker

data = open("tune.swm", "rb").read()
sid = sidmaker.export(data)                   # SID-Maker 1.97, 8580 in the header
image = sidmaker.tune(data, "1.8")            # a pysidtracker SidImage of the export
session = sidmaker.SidMaker("1.8")            # successive exports share its memory
```

`scripts/build_sidmaker.py` rebuilds the binaries from each release's sources and
proves each is the release's own SID-Maker. The 1.94/1.97 example `.sid` exports are
the 1.8 release's files; SID-Maker 1.8 reproduces them byte for byte.

## Play an SWM

```python
from pysidwizard import SWMPlayer, read_swm

swm = read_swm("tune.swm")
player = SWMPlayer(swm)

# Each play_frame() returns the SID register writes the real SID-Wizard player
# would emit this frame, in order (channel 3 -> 2 -> 1, then global filter + volume).
for _ in range(100):
    for reg, value in player.play_frame():
        print(f"${reg:04X} = ${value & 0xFF:02X}")
```

### Unmodelled effects

An effect the player does not implement is never silently swallowed: it warns
once per `(column, code)` with `SWMUnsupportedEffectWarning` and is tallied in
`player.unsupported_effects`. Promote it to a hard failure with
`warnings.simplefilter("error", SWMUnsupportedEffectWarning)`.

```python
import warnings
from pysidwizard import SWMPlayer, SWMUnsupportedEffectWarning, read_sid

player = SWMPlayer(read_sid("tune.sid"))
with warnings.catch_warnings():
    warnings.simplefilter("ignore", SWMUnsupportedEffectWarning)
    for _ in range(3000):
        player.play_frame()
print(player.unsupported_effects)  # {("big-fx", 6): 12, ("small-fx", 194): 3}
```

### Render to WAV

Use the shared `pysidtracker` CLI (registered for `.sid` files via the
`SidFormat` entry point):

```bash
pysidtracker wav tune.sid out.wav --seconds 60 --model 8580
```

The CLI operates on `.sid` files (not `.swm`) and drives
[`pyresidfp`](https://pypi.org/project/pyresidfp/) (install with
`pip install pyresidfp`).

### Iterate register writes

```python
from pysidwizard import iter_register_writes

for clock, reg, value in iter_register_writes(swm, max_frames=1500):
    ...  # reg is a 0..0x18 register offset
```

`iter_register_writes` yields `RegWrite(clock, reg, val)` for the registers that
change each frame. For a full per-frame 25-register grid, use
`SWMPlayer(swm).render_grid(n)`, which returns `n` forward-filled rows.

## HVSC oracle survey

[`scripts/oracle_hvsc.py`](../scripts/oracle_hvsc.py) plugs `SWMPlayer` into the
[`pysidtracker.survey`](https://github.com/anarkiwi/pysidtracker/blob/main/docs/survey.md)
whole-song comparison against the `sidtrace` oracle (Docker), over every subtune
of every HVSC SID-Wizard tune. Its `info` reports the export's `driver` build and
`frame_speed`, `rsid`, the play `trigger` (`pal_video` / `ntsc_video` /
`cia_timer`, or `null` when unknown), `speed` (play calls per PAL frame) and
`dynamic` (variable timer), the last four from
`pysidtracker.cadence.cadence_info`. Tunes `parse_sid` rejects (one player per
chip, unresolved layouts) and multi-SID modules `SWMPlayer` cannot render raise
`SIDFormatError` / `SWMFormatError`, a `pysidtracker.SidParseError`, so the survey
counts them as `not_decompiled`; `survey scope` lists that bucket (and any
`library_error`) without Docker.

```bash
pip install "pysidtracker[survey]>=0.24.0"
python -m pysidtracker.survey find $HVSC --player sidwizard --out .hvsc-survey/tunes.csv
PYTHONPATH=scripts python -m pysidtracker.survey run $HVSC .hvsc-survey/tunes.csv \
    --work .hvsc-survey --render oracle_hvsc:render --jobs 32
python -m pysidtracker.survey report .hvsc-survey/results.csv
python -m pysidtracker.survey scope $HVSC .hvsc-survey/tunes.csv \
    --render scripts/oracle_hvsc.py --out .hvsc-survey/scope.csv
python -m pysidtracker.survey triage .hvsc-survey
PYTHONPATH=scripts python -m pysidtracker.survey diff $HVSC .hvsc-survey/tunes.csv <md5>_<song> \
    --work .hvsc-survey --render oracle_hvsc:render
```

[`scripts/feature_census.py`](../scripts/feature_census.py) lists, per build flag,
the tunes whose player code disagrees with their header's driver byte:

```bash
python scripts/feature_census.py tunes.txt --jobs 32 --csv .hvsc-survey/features.csv
```

### pathdiff xfails

[`pysidtracker.pathdiff`](https://github.com/anarkiwi/pysidtracker/blob/main/docs/pathdiff.md)
localises the survey's divergences to the fewest fixes, comparing jennings running
each tune (`6502`) with the `sidwizard` format. `tests/pathdiff_xfails.json` holds
one case per top fix; `tests/test_pathdiff.py` runs each as a strict xfail against
the oracle (`-m oracle`). Regenerate it after a survey:

```bash
python -m pysidtracker.pathdiff build $HVSC .hvsc-survey/tunes.csv --work .hvsc-survey \
    --left 6502 --right format:sidwizard --jobs 32
python -m pysidtracker.pathdiff rank --work .hvsc-survey --right format:sidwizard --explain 10
python -m pysidtracker.pathdiff xfails --work .hvsc-survey --right format:sidwizard \
    --manifest tests/pathdiff_xfails.json --top 15 --verify --prune
```

`build` reads `results.csv`'s `info`, so run the survey with `--render
scripts/oracle_hvsc.py:render` (its `driver`, `frame_speed` and cadence fields become
`info:` build-option features), and pass `--force` after a pysidtracker upgrade
changes the cache format. Existing case ids are kept and `--prune` drops fixed cases; delete the
manifest first to rebuild it from the current ranking only. CI pre-fetches its tunes
(`pysid-tune-fetch --tests tests/pathdiff_xfails.json`).

`.hvsc-survey/` holds copyright-derived traces and is gitignored. When
`.hvsc-survey/tunes.csv` (or `$PYSIDWIZARD_TUNES_CSV`) and the HVSC tree
(`$HVSC`, default `/scratch/datasets/hvsc/C64Music`) are present, the unit suite
also parses and plays every listed tune.
