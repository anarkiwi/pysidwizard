# pysidwizard

Pure-Python reader, writer, and player for
[SID-Wizard](https://csdb.dk/release/?id=258573) (Hermit / Mihaly Horvath) C64
tracker modules. Implements the SWM file format from first principles. The player
is the SID-Wizard playroutine stated as a
[`pysidtracker.playspec`](https://github.com/anarkiwi/pysidtracker) spec, run on the
tune's own 6502 image (a bare module's SID-Maker export), and is checked against
that 6502 and the [`anarkiwi/sidtrace`](https://github.com/anarkiwi/sidtrace)
sidplayfp oracle over real HVSC SID-Wizard exports (`pytest -m oracle`, needs Docker).

Also consumes `.sid` files (PSID/RSID containers), SID-Maker `.exe.prg` and Echo `.sid`
exports (unwrapped to the `.sid` they embed) and bare `.prg` images through
the shared [`pysidtracker`](https://github.com/anarkiwi/pysidtracker) base:
SID-Wizard `.sid` exports (plain or packed `SWP`, single- or multi-SID) are read into the same
`SWMFile` model, packed/relocating builds are detected, and container headers
are not trusted. Standalone packed `.swp` files (`SWP1`) are read with the same
decodable fidelity. The `.sid`/`.swp` paths are read-only and lossy (see
[docs/format.md](docs/format.md)).

## Install

```bash
pip install pysidwizard
```

## Usage

```python
from pysidwizard import read_swm, write_swm, build_swm

swm = read_swm("tune.swm")            # native SID-Wizard module (bare or PRG-wrapped)
print(swm.author_str(), swm.frame_speed, swm.highlight)

for row in swm.patterns[0].rows[:4]:  # typed rows; no byte poking
    print(row)

write_swm(swm, "out.swm")             # byte-exact round-trip

# SID-Wizard tunes exported to .sid read into the same model:
from pysidwizard import read_sid
swm_from_sid = read_sid("tune.sid")   # read-only, lossy

# ...as do standalone packed .swp files (SWP1):
from pysidwizard import read_swp
swm_from_swp = read_swp("tune.swp")   # read-only, lossy
```

See [docs/usage.md](docs/usage.md) for building modules from scratch, playback,
WAV rendering, raw write iteration, and the CLI, and
[docs/format.md](docs/format.md) for the SWM/`.sid`/`.swp` format, data model,
and player correctness. The [HVSC oracle survey](docs/usage.md#hvsc-oracle-survey)
runs the player against every HVSC SID-Wizard tune. [docs/playspec.md](docs/playspec.md)
describes the playroutine stated as a `pysidtracker.playspec` spec.

## Development

```bash
pip install -e ".[dev]"
python -m pytest                                   # unit tests (fast, no Docker)
python -m pytest -m oracle                          # sidtrace oracle; requires Docker
```

The four SWM test tunes are not tracked in this repo (SID-Wizard binary
artifacts); `tests/_swm_cache.py` fetches them on demand into pysidtracker's
download cache (`.tunecache`, `$PYSID_TUNECACHE`) and SHA-256 verifies each one.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
