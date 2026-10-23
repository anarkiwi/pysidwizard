"""Byte-exact comparison of the native SWMPlayer against the sidtrace oracle.

Marked ``oracle``: these tests need Docker (the ``anarkiwi/sidtrace`` image) and
HVSC ``.sid`` tunes, so the default suite excludes them (see ``pyproject``); a
dedicated CI job runs ``pytest -m oracle``. They are never skipped -- an
unavailable tune or a failed oracle render fails the test rather than hiding a
regression. HVSC ``.sid`` files are copyright works: they are downloaded to a
cache (or a local ``$HVSC`` tree), never committed.

The sidtrace oracle renders a PSID/RSID container through ``sidplayfp``; the four
bundled SID-Wizard example tunes are raw ``.swm`` modules it cannot read, so the
native player is validated against real HVSC SID-Wizard ``.sid`` exports instead.
:data:`TUNES` is the curated set of such exports whose native
:class:`~pysidwizard.player.SWMPlayer` render matches the oracle frame-for-frame.

The native player models the driver's INITER state and the TICK_0 / TICK_1 setup
frames of the first row, so its render matches the oracle from the first play
call: no leading frame is trimmed or excused.
"""

import sys
from pathlib import Path

import pytest
from pysidtracker.oracle import register_grid
from pysidtracker.testing import (
    TuneFetchError,
    assert_oracle_match,
    make_oracle_fixtures,
    oracle_grid,
    resolve_tune,
)

from pysidwizard import SWMPlayer, parse_sid_song

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import oracle_hvsc  # noqa: E402  (after sys.path tweak)

# whydiff reports for a mismatch (not a download, so outside the fetch cache).
_WHY = Path("build/why")

# Frames compared per tune.
FRAMES = 250

# HVSC SID-Wizard exports the native player reproduces byte-exactly against the
# deterministic sidtrace oracle (curated from tests/_hvsc_corpus.SAMPLE).
TUNES = {
    "8_bit_bard": "DEMOS/0-9/8-Bit_Bard.sid",
    # Exercises BIGFX05 (WRITEAD) alongside BIGFX02/03; matched only once the
    # $04/$05/$06 register-write effects were modelled.
    "cloud_9": "MUSICIANS/A/Ash_9/Cloud_9.sid",
    "oua_oua_song": "DEMOS/M-R/Oua_Oua_Song.sid",
    "pentagorat_ii": "GAMES/M-R/Pentagorat_II.sid",
    "burn": "MUSICIANS/D/DAM/Burn.sid",
    "must_dash": "MUSICIANS/D/DAM/Must_Dash.sid",
    "taming_the_fire": "MUSICIANS/D/Dave_Sidnify/Taming_the_Fire.sid",
    "ready_to_go": "MUSICIANS/G/Goerp/Ready_to_Go.sid",
    "csillag": "MUSICIANS/H/Hermit/Csillag_szazketto_kettoskereszt.sid",
    "rodman_jr_plus": "MUSICIANS/M/Misfit/Rodman_Jr_plus.sid",
    "death_in_rome": "MUSICIANS/S/Stone_James/Death_in_Rome.sid",
    "star_storm": "GAMES/S-Z/Star_Storm.sid",
    # Exercises BIGFX11 (main funktempo); matched only once the tempo FX landed.
    "moofistication": "MUSICIANS/S/Skuggemannen/Moofistication.sid",
    "cyberiad_theory": "MUSICIANS/L/LukHash/Cyberiad_Theory.sid",
    "perpetual_motion": "MUSICIANS/L/LukHash/Perpetual_Motion.sid",
    "switchback_remake": "MUSICIANS/V/Vincenzo/Switchback_Remake.sid",
    # Driver type 3, the Extra build (altplayers.inc:533): matched only once FASTSPEEDBIND / ALLGHOSTREGS / VIBSLIDEALWAYS were modelled.
    "bad_bagel": "DEMOS/A-F/Bad_Bagel.sid",
    "eggshell": "MUSICIANS/A/Archmage/Eggshell.sid",
    "still_time": "MUSICIANS/A/Avrilcadabra/Still_Time.sid",
    "portugieser": "MUSICIANS/B/Barik/Portugieser.sid",
    "vinegar": "MUSICIANS/B/Barik/Vinegar.sid",
    "wattson": "MUSICIANS/B/Barik/Wattson.sid",
    # Driver type 1, the Medium build (altplayers.inc:331): matched once HARDRESTYPES / FRAME1SWITCH / PWKEYBTRACK / FILT_CTRL_FX were gated off.
    "seuck_camp": "MUSICIANS/M/Mac_TUGCS/SEUCK_Camp.sid",
    "captain_alehouse": "MUSICIANS/M/Max_F3H/Captain_Alehouse.sid",
    "spongebog": "MUSICIANS/M/Max_F3H/SpongeBog.sid",
    "soy_un_delincuente_2": "MUSICIANS/M/Morton_Adam/Soy_un_Delincuente_tune_2.sid",
    "probable_harmonies": "MUSICIANS/S/Slash/Probable_Harmonies.sid",
    "easyriff": "MUSICIANS/S/Spider_Jerusalem/Easyriff.sid",
    # Driver type 2, the Light build (altplayers.inc:432): matched once OCTAVESHIFT / DETUNESUPPORT / CALCVIBRATO / FINEFILTSWEEP / PWRESETSW / FILTRESETSW / CHORDSUPPORT were gated off.
    "mega_run": "MUSICIANS/A/Arturo_Dente/Mega-Run.sid",
    "cracked": "MUSICIANS/D/Deetsay/Cracked.sid",
    "negyedik_emelet": "MUSICIANS/H/Hermit/4_Emelet.sid",
    "mid_of_ages": "MUSICIANS/K/Kompositkrut/Mid_of_Ages.sid",
    "cloudless_rain": "MUSICIANS/R/Rousseau_Marcus/Cloudless_Rain.sid",
    "action_level": "MUSICIANS/S/Spider_Jerusalem/Action_Level.sid",
    "sad_ending": "MUSICIANS/S/Spider_Jerusalem/Sad_Ending.sid",
    # Driver type 4, the Bare build (altplayers.inc:634): every optional feature off, so it inherits the Medium + Light gates without needing any of its own.
    "lykia_boss": "MUSICIANS/C/C0zmo/Lykia_Boss.sid",
    "lykia_forest": "MUSICIANS/C/C0zmo/Lykia_Forest.sid",
    "lykia_tower": "MUSICIANS/C/C0zmo/Lykia_Tower.sid",
    "kicks_like_a_mule": "MUSICIANS/T/Toggle/Kicks_Like_a_M_U_L_E.sid",
    "wintry_haze": "MUSICIANS/T/Toggle/Wintry_Haze.sid",
    "supremacy_intro": "MUSICIANS/T/Toggle/Supremacy_Intro.sid",
    "juana_azurduy": "MUSICIANS/U/Uctumi/Juana_Azurduy_Reggae_Mix.sid",
    # Driver type 5, the Demo build (altplayers.inc:735): its flags are a subset of the Medium + Light sets, so it needs no gating of its own.
    "methodist": "MUSICIANS/M/Mac_TUGCS/Methodist.sid",
    "smooth_and_wonders": "MUSICIANS/V/Vincenzo/Smooth_and_Wonders.sid",
    "unfortunate_coincidence": "MUSICIANS/V/Vincenzo/Unfortunate_Coincidence.sid",
    # WF-arp relative steps whose sum leaves the 96-note table (masked indices 97 / 107 / 109 / 117): matched only once ABSPTCH's ``and #$7F`` replaced a clamp and the tunes' own past-FREQTBL image bytes backed the lookup.
    "argonaut": "MUSICIANS/A/AMB/Argonaut.sid",
    "memory_invaders": "MUSICIANS/C/Chiummo_Gaetano/Memory_Invaders.sid",
    # Extra build: an abandoned WF-table jump keeps ``cmp #$FE``'s carry, and PORTEND restores the instrument vibrato.
    "your_fathers_nightmare": "GAMES/S-Z/Your_Fathers_Nightmare.sid",
}


def _render(data, nframes):
    return oracle_hvsc.render(data, 1, nframes)[0].tolist()


def _player(data):
    return SWMPlayer(parse_sid_song(data, 1))


# A mismatch fails with pysidtracker's whydiff report, also saved under ``build/why/``.
tune_id, oracle_match = make_oracle_fixtures(
    TUNES,
    player=_player,
    frames=FRAMES,
    max_lead=0,
    report_dir=_WHY,
)


@pytest.mark.oracle
def test_native_render_matches_oracle(oracle_match):  # noqa: F811
    """The native render equals the oracle grid from the first play call on."""
    oracle_match()


@pytest.mark.oracle
def test_gate_off_without_instrument_selects_legatoo_route():
    """Gate-off note-FX without a selected instrument goes to LEGATOO, not NGATEOF.

    TICK_2's ``ldy CURINS,x; bne STRTINS`` (player.asm:1326) skips the whole
    note column otherwise; without that guard this tune first diverges at
    frame 264, past the default 250-frame window above.
    """
    assert_oracle_match(
        "MUSICIANS/B/Batsman/Propaganda.sid",
        _player,
        frames=FRAMES + 50,
        report_dir=_WHY,
    )


# WF-arp relative steps whose sum leaves the 96-note table (masked indices 97 / 107 / 109 / 117).
# The tune's own RSID raster IRQ ($0948) calls a play routine that first copies the
# previous call's ghost registers ($0800-$0818) to the SID, so each driver frame
# reaches the chip one interrupt later and the first interrupt writes nothing.
DELAYED_TUNE = "MUSICIANS/D/Deetsay/1_2_3_C64.sid"


@pytest.mark.oracle
def test_ghost_buffered_rsid_matches_oracle_one_interrupt_late():
    path = resolve_tune(DELAYED_TUNE)
    if path is None:
        raise TuneFetchError(f"{DELAYED_TUNE} unavailable (offline, not cached)")
    expected = oracle_grid(path, seconds=FRAMES // 50 + 2, frames=FRAMES + 1)
    assert expected[0] == [0] * len(expected[0])
    assert expected[1:] == _render(Path(path).read_bytes(), FRAMES)


# Songs whose init maps the header song to another subtune or to another
# embedded player's module (sidreader.parse_sid_song): (HVSC path, 1-based song).
SONGS = {
    "star_storm_5": ("GAMES/S-Z/Star_Storm.sid", 5),
    "prex_2nd_player_4": ("GAMES/M-R/PREX.sid", 4),
    "prex_2nd_player_9": ("GAMES/M-R/PREX.sid", 9),
    "farming_simulator_1": ("MUSICIANS/V/Vincenzo/Farming_Simulator_6581.sid", 1),
}


@pytest.mark.oracle
@pytest.mark.parametrize("song_id", list(SONGS))
def test_song_render_matches_oracle(song_id):
    relpath, song = SONGS[song_id]
    assert_oracle_match(
        relpath,
        lambda data: SWMPlayer(parse_sid_song(data, song)),
        frames=FRAMES,
        song=song,
        report_dir=_WHY,
    )


@pytest.mark.oracle
def test_orderlist_end_leaves_the_last_row_unplayed_on_the_6502():
    """PREX song 4 (the Light build) ends voice 3 on a gate-off row that the
    6502 never applies. Its sidtrace grid stops at the last register change,
    before that row, so the jennings grid is the reference."""
    path = resolve_tune("GAMES/M-R/PREX.sid")
    if path is None:
        raise TuneFetchError("GAMES/M-R/PREX.sid unavailable (offline, not cached)")
    data = Path(path).read_bytes()
    expected = register_grid(data, 2 * FRAMES, subtune=3)
    assert oracle_hvsc.render(data, 4, 2 * FRAMES)[0].tolist() == expected
