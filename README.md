# pysidwizard

Pure-Python reader, writer, and bit-exact player for
[SID-Wizard](https://csdb.dk/release/?id=258573) (Hermit / Mihaly Horvath) C64
tracker modules. Implements the SWM file format and player IRQ from first
principles, with no native dependencies. The player is verified against a live
capture of SID-Wizard running inside `asid-vice`: every frame × every SID
register, on every PR.

Also consumes `.sid` files (PSID/RSID containers) and bare `.prg` images through
the shared [`pysidtracker`](https://github.com/anarkiwi/pysidtracker) base:
SID-Wizard `.sid` exports (plain or packed `SWP`) are read into the same
`SWMFile` model, packed/relocating builds are detected, and container headers
are not trusted. The `.sid` path is read-only and lossy (see
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
```

See [docs/usage.md](docs/usage.md) for building modules from scratch, playback,
WAV rendering, raw write iteration, and the CLI, and
[docs/format.md](docs/format.md) for the SWM/`.sid` format, data model, and
player correctness.

## Development

```bash
pip install -e ".[dev]"
python -m pytest                                   # unit tests (fast, no Docker)
pip install -e ".[integration]"
python -m pytest -m integration tests/integration/  # slow; requires Docker
```

The four SWM test tunes are not tracked in this repo (SID-Wizard binary
artifacts); `tests/_swm_cache.py` fetches them on demand and SHA-256 verifies
each one.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
