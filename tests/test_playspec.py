"""The SID-Wizard playspec: whole songs against the tune's own 6502 and against itself."""

# pylint: disable=redefined-outer-name

import json
import sys
from pathlib import Path

import pytest
from pysidtracker import SidImage
from pysidtracker.disasm import image_after_init
from pysidtracker.header import parse_sid_header
from pysidtracker.maincli import main as cli
from pysidtracker.playspec import SpecPlayer
from pysidtracker.testing import make_tune_fixtures, resolve_tune, song_frames

from pysidwizard import playspec
from tests._refused import REASONS, REFUSED
from tests._swm_cache import SW197_EXPORT_SHA256, sw197_export_path
from tests.test_oracle import SONGS, TUNES

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import sw_spec_diff  # noqa: E402  # pylint: disable=wrong-import-position,wrong-import-order

EXPORTS = ("bronkosaurus", "euphoria", "flashitback", "glacialblues")
_SID_EXPORTS = "native/examples/sid-exports/"
ALL_EXPORTS = tuple(Path(m).stem for m in SW197_EXPORT_SHA256 if m.startswith(_SID_EXPORTS))
# Players the binder refuses, by the reason it gives.
REFUSED_EXPORTS = {name: "multi-SID" for name in ALL_EXPORTS if name.endswith("SID")}
REFUSED_TUNES = {k: REASONS[REFUSED[v][1]] for k, v in TUNES.items() if 1 in REFUSED.get(v, {})}
STATED_TUNES = {k: v for k, v in TUNES.items() if k not in REFUSED_TUNES}

tune_id, tune_path = make_tune_fixtures(STATED_TUNES)


def _export(name: str) -> bytes:
    return sw197_export_path(f"{_SID_EXPORTS}{name}.sid").read_bytes()


def _view(data: bytes) -> bytes:
    """The whole memory after the tune's init."""
    return bytes(image_after_init(SidImage.from_sid(data)).window(0, 0x10000))


def _tune_view(name: str) -> bytes:
    return _view(Path(resolve_tune(TUNES[name])).read_bytes())


def _word(view: bytes, at: int) -> int:
    return view[at] | view[at + 1] << 8


@pytest.mark.parametrize("name", EXPORTS)
def test_the_voices_initial_state_is_read_seven_bytes_apart(name):
    """Each voice's variable starts as the byte the init left at the found address plus seven
    bytes per voice."""
    view = _view(_export(name))
    found, at = playspec.binding(view), playspec.labels(view)
    for var in ("curval", "spdcnt", "transp", "dpitch"):
        assert found[f"init_{var}"] == [view[at[var] + 7 * voice] for voice in range(3)]


@pytest.mark.parametrize("name", ALL_EXPORTS)
def test_spec_sounds_as_the_6502_over_the_whole_song(name):
    """SpecPlayer over the spec and the tune's own play routine leave the SID registers, and
    the gate and test edges, equal after every call of the song."""
    data = _export(name)
    if name in REFUSED_EXPORTS:
        with pytest.raises(ValueError, match=REFUSED_EXPORTS[name]):
            playspec.binding(_view(data))
        return
    assert sw_spec_diff.first_difference(data, song_frames(data, 1), progress=False) is None


@pytest.mark.parametrize("name", [n for n in ALL_EXPORTS if n not in REFUSED_EXPORTS])
def test_native_and_python_execution_of_the_spec_agree(name):
    """The spec run natively and run in Python leave the same SID writes, registers and
    memory outside the stack page after every call."""
    data = _export(name)
    image, header = SidImage.from_bytes(data), parse_sid_header(data)
    play = header.play_address or sw_spec_diff.play_entry(image)
    args = (image.image, image.load, header.init_address, play, 0)
    made = [
        SpecPlayer(
            playspec.SPEC,
            *args,
            bind=playspec.binder(),
            entries=playspec.entries,
            psid=header.is_psid or not header.play_address,
            native=native,
        )
        for native in (True, False)
    ]
    for _ in range(min(song_frames(data, 1), 150)):
        for player in made:
            player.play_frame()
        fast, slow = made
        assert fast.writes == slow.writes
        assert fast.regs == slow.regs
        assert bytes(fast.memory[:0x100]) + bytes(fast.memory[0x200:]) == (
            bytes(slow.memory[:0x100]) + bytes(slow.memory[0x200:])
        )


@pytest.mark.parametrize("name", [n for n in ALL_EXPORTS if n not in REFUSED_EXPORTS])
def test_the_cli_checks_the_spec_with_a_bound_layout(name, tmp_path, capsys):
    """``pysidtracker playspec check`` accepts the spec bound to each export's layout."""
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps(playspec.binding(_view(_export(name)))))
    spec = Path(playspec.__file__).parent / "specs" / "sidwizard.play"
    assert not cli(["playspec", "check", str(spec), "--binding", str(binding)])
    out = capsys.readouterr().out
    assert out.startswith("ok: sidwizard; order play, voice[2], voice[1], voice[0], commonregs")


def test_entries_are_the_players_play_and_multispeed_calls():
    """PLAYER and MULPLY of the player, as its jump table names them."""
    data = _export("bronkosaurus")
    view = _view(data)
    table = SidImage.from_sid(data).load
    assert playspec.entries(view) == {_word(view, table + 4), _word(view, table + 7)}


@pytest.mark.oracle
@pytest.mark.parametrize("name", ("lykia_boss", "star_storm"))
def test_an_sfx_build_has_sfxinit_as_an_entry(name):
    """SFXsub's jump lands on SFXinit, which binds as its own entry."""
    data = Path(resolve_tune(TUNES[name])).read_bytes()
    view = _view(data)
    sfxsub = SidImage.from_sid(data).load + 12
    target = _word(view, sfxsub + 1)
    assert target in playspec.entries(view)
    assert playspec.binding(view, target)["sfxinit"]
    assert not playspec.binding(view)["sfxinit"]
    assert not playspec.binding(_view(_export("bronkosaurus")))["sfxinit"]


@pytest.mark.oracle
def test_a_pulse_width_effect_stored_outside_the_player_is_a_memory_write():
    """prex_2nd_player's bigfx $0E stores at an address that is not its pulse width byte."""
    path, song = SONGS["prex_2nd_player_4"]
    player, _cpu = sw_spec_diff.players(Path(resolve_tune(path)).read_bytes(), song)
    player.play_frame()
    layout = playspec.binding(player.view(), player.entry)
    assert layout["bigfx_pwhi_stray"]
    assert layout["bigfx_pwhi"] != playspec.labels(player.view(), player.entry)["pwhigho"]
    assert not playspec.binding(_view(_export("bronkosaurus")))["bigfx_pwhi_stray"]


@pytest.mark.oracle
def test_spec_sounds_as_the_6502_for_every_oracle_tune(tune_path):  # noqa: F811
    """Every call of every stated oracle tune's whole song leaves both players equal."""
    data = tune_path.read_bytes()
    assert sw_spec_diff.first_difference(data, song_frames(data, None), progress=False) is None


@pytest.mark.oracle
@pytest.mark.parametrize("name", ("lykia_boss", "star_storm"))
def test_sound_effects_of_an_sfx_build_equal_the_6502(name):
    """A game's SFXinit calls (SFXsub, the jump table's fifth entry) start sound effects
    on the third voice: every call of the song still leaves both players equal."""
    data = Path(resolve_tune(TUNES[name])).read_bytes()
    sfxsub = SidImage.from_sid(data).load + 12
    calls = {
        f: (sfxsub, (0x18 + f % 0x40, 0x20 + f % 0x30, 1 + f % 5)) for f in range(40, 3000, 97)
    }
    frames = song_frames(data, None)
    assert sw_spec_diff.first_difference(data, frames, calls=calls, progress=False) is None


@pytest.mark.oracle
@pytest.mark.parametrize("name", REFUSED_TUNES)
def test_players_the_spec_does_not_state_are_refused(name):
    """The binder names why it does not bind each player the spec does not state."""
    view = _tune_view(name)
    with pytest.raises(ValueError, match=REFUSED_TUNES[name]):
        playspec.binding(view)
