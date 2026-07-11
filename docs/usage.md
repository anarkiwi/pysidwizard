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

This path is read-only and lossy (see [format.md](format.md#sid-wizard-sid-export-read-only-lossy)).

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
