"""HVSC SID-Wizard songs the playspec binder refuses, each with the issue that tracks it.

Written by ``scripts/gen_refused.py`` from ``scripts/sw_refusals.py``.
"""

import pytest

from pysidwizard import SIDFormatError, SWMPlayer

# A refused song raises within this many play calls (a wrapper may reach PLAYER late).
CALLS = 60
# The binder's refusal text for each issue's builds.
REASONS = {
    121: "partly relocated player is not stated",
    122: "no SID-Wizard big effect",
    123: "no SID-Wizard",
    124: "no SID-Wizard",
    125: "multi-SID",
}

REFUSED = {
    "DEMOS/A-F/Devils_ReSIDence_3SID.sid": {1: 125},
    "DEMOS/A-F/Diving_into_You.sid": {1: 121},
    "DEMOS/M-R/Przepis_na_kopytka.sid": {1: 121},
    "DEMOS/S-Z/TES_IV_Oblivion_2SID.sid": {1: 125},
    "GAMES/A-F/Castle_Shadowgate.sid": {1: 121},
    "GAMES/G-L/Gravitrix.sid": {1: 121, 2: 121, 3: 121},
    "GAMES/G-L/Immensity_preview_2.sid": {1: 123},
    "MUSICIANS/A/AMB/Kd0s.sid": {1: 121},
    "MUSICIANS/A/Ant1/My_Rules.sid": {1: 121},
    "MUSICIANS/A/Ant1/Techno_2.sid": {1: 124},
    "MUSICIANS/A/Arturo_Dente/Mega-Run.sid": {1: 121},
    "MUSICIANS/B/Batsman/Lost_in_a_Loop.sid": {1: 121},
    "MUSICIANS/B/Batsman/Piraten_2SID.sid": {1: 125},
    "MUSICIANS/C/C0zmo/Autumn_Skies.sid": {1: 122},
    "MUSICIANS/C/C0zmo/Oldschool_Heaven.sid": {1: 122},
    "MUSICIANS/C/C0zmo/Space_Oddity_2SID.sid": {1: 125},
    "MUSICIANS/C/Chabee/Stickman_2SID.sid": {1: 125},
    "MUSICIANS/C/Chiummo_Gaetano/A_Childhood_Dream_2SID.sid": {1: 125},
    "MUSICIANS/C/Chiummo_Gaetano/Calming_Stream_2SID.sid": {1: 125},
    "MUSICIANS/C/Chiummo_Gaetano/Dreamworld.sid": {1: 121},
    "MUSICIANS/C/Chiummo_Gaetano/Edelin_Tales_Theme_3SID.sid": {1: 125},
    "MUSICIANS/C/Chiummo_Gaetano/Enchanted_Forest_3SID.sid": {1: 125},
    "MUSICIANS/C/Chiummo_Gaetano/Happy_New_Wave_3SID.sid": {1: 125},
    "MUSICIANS/C/Chiummo_Gaetano/Hope_3SID.sid": {1: 125},
    "MUSICIANS/C/Chiummo_Gaetano/Memory_Invaders.sid": {1: 121},
    "MUSICIANS/C/Chiummo_Gaetano/Ramos_2016_3SID.sid": {1: 125},
    "MUSICIANS/D/DAM/Ember_2SID.sid": {1: 125},
    "MUSICIANS/D/DAM/In_Your_Room.sid": {1: 121},
    "MUSICIANS/D/DAM/New_Air_2SID.sid": {1: 125},
    "MUSICIANS/D/Deetsay/Punish_Da_Hippo.sid": {1: 124},
    "MUSICIANS/E/Et1999cc/Flowerguys_Pool_Party_2SID.sid": {1: 125},
    "MUSICIANS/E/Et1999cc/Resonance_3SID.sid": {1: 125},
    "MUSICIANS/G/Goerp/Kung_Fu_Pico.sid": {1: 122, 2: 122, 3: 122, 4: 122, 5: 122, 6: 122},
    "MUSICIANS/G/Gregfeel/Lost_in_Time.sid": {1: 122},
    "MUSICIANS/H/Hermit/E_G_Blues_2SID.sid": {1: 125},
    "MUSICIANS/H/Hermit/Emomyst.sid": {1: 123},
    "MUSICIANS/H/Hermit/Phonky_2SID.sid": {1: 125},
    "MUSICIANS/H/Hermit/Pimp_My_Commodore.sid": {1: 124},
    "MUSICIANS/H/Hermit/Song_of_the_Second_Moon_Delta_2SID.sid": {1: 125},
    "MUSICIANS/H/Hermit/Thiz_Iz_Da_Gizda_3SID.sid": {1: 125},
    "MUSICIANS/H/Hermit/Tree_Angel_3SID.sid": {1: 125},
    "MUSICIANS/K/Kompositkrut/Mid_of_Ages.sid": {1: 121},
    "MUSICIANS/K/Kompositkrut/Out_of_Range.sid": {1: 121},
    "MUSICIANS/K/Kompositkrut/Rage_Quit_Loop.sid": {1: 121},
    "MUSICIANS/M/M_O_T/Take_em_Out_stereo_2SID.sid": {1: 125},
    "MUSICIANS/M/Mac_TUGCS/Crystal_Creatures_stereo_remix_2SID.sid": {1: 125},
    "MUSICIANS/M/Mario64/Club64.sid": {1: 121},
    "MUSICIANS/M/Mario64/Endstation.sid": {1: 121},
    "MUSICIANS/M/Mario64/Messy_Messi.sid": {1: 121},
    "MUSICIANS/M/Moellpauk/Frantic4BHF_tune_2.sid": {1: 121},
    "MUSICIANS/M/Moellpauk/Groms.sid": {1: 121},
    "MUSICIANS/M/Moellpauk/Too_Long_on_Disk.sid": {1: 121},
    "MUSICIANS/N/NecroPolo/Bricks_n_Balls.sid": {1: 121, 2: 121, 3: 121, 4: 121, 5: 121, 6: 121},
    "MUSICIANS/N/NecroPolo/Excess_Intro.sid": {1: 121},
    "MUSICIANS/N/NecroPolo/Get_to_the_End.sid": {
        1: 121,
        2: 124,
        3: 124,
        4: 124,
        5: 124,
        6: 124,
        7: 124,
        8: 124,
        9: 124,
    },
    "MUSICIANS/N/Nobody/Hardtek_Jam_2SID.sid": {1: 125},
    "MUSICIANS/N/Nobody/Turn_it_Louder_2SID.sid": {1: 125},
    "MUSICIANS/O/Onebitman/Deafender.sid": {1: 124},
    "MUSICIANS/R/Razy/Bereft.sid": {1: 121},
    "MUSICIANS/S/SIDwave/Alterations_2SID.sid": {1: 125},
    "MUSICIANS/S/SIDwave/Arrow_of_Time_2SID.sid": {1: 125},
    "MUSICIANS/S/SIDwave/Broken_Dreams_2SID.sid": {1: 125},
    "MUSICIANS/S/SIDwave/Electric_Dancer_2SID.sid": {1: 125},
    "MUSICIANS/S/SIDwave/Jacked_up_2SID.sid": {1: 125},
    "MUSICIANS/S/SIDwave/Kitaros_Dream_2SID.sid": {1: 125},
    "MUSICIANS/S/SIDwave/Open_the_Box_2SID.sid": {1: 125},
    "MUSICIANS/S/SIDwave/Rough_Racer_Menu_2SID.sid": {1: 125},
    "MUSICIANS/S/Skuggemannen/Moofistication.sid": {1: 121},
    "MUSICIANS/S/Spider_Jerusalem/Canon_457_Study_2_2SID.sid": {1: 125},
    "MUSICIANS/S/Spider_Jerusalem/Easyriff.sid": {1: 121},
    "MUSICIANS/S/Spider_Jerusalem/Gerry_the_Druid.sid": {1: 121, 2: 121},
    "MUSICIANS/S/Spider_Jerusalem/Impossible_Game.sid": {1: 121},
    "MUSICIANS/S/Spider_Jerusalem/Trap_Them_64.sid": {1: 121},
    "MUSICIANS/S/Spider_Jerusalem/Vermaledeit4K.sid": {1: 121},
    "MUSICIANS/S/Stone_James/A_Final_Hyperbase_2SID.sid": {1: 125},
    "MUSICIANS/S/Stone_James/Blazon_Intro.sid": {1: 121},
    "MUSICIANS/S/Stone_James/Dawn_of_the_Oval_Hordes_2SID.sid": {1: 125},
    "MUSICIANS/T/Televicious/Aliens_2SID.sid": {1: 125},
    "MUSICIANS/T/Televicious/Cybo_2SID.sid": {1: 125},
    "MUSICIANS/T/Televicious/Eviltwin_2SID.sid": {1: 125},
    "MUSICIANS/T/Televicious/Fidelity_2SID.sid": {1: 125},
    "MUSICIANS/T/Televicious/Finity_2SID.sid": {1: 125},
    "MUSICIANS/T/Televicious/Giovanni_2SID.sid": {1: 125},
    "MUSICIANS/T/Televicious/Ring-a-sync_2SID.sid": {1: 125},
    "MUSICIANS/T/Televicious/Sauceage_2SID.sid": {1: 125},
    "MUSICIANS/T/Televicious/Symphonic_2SID.sid": {1: 125},
    "MUSICIANS/T/Toggle/Beacon_2SID.sid": {1: 125},
    "MUSICIANS/V/Vincenzo/Farming_Simulator_6581.sid": {2: 121},
    "MUSICIANS/V/Vincenzo/Farming_Simulator_8580.sid": {2: 121},
    "MUSICIANS/V/Vincenzo/FreeFall.sid": {1: 121},
    "MUSICIANS/W/Wemyss_Chris/Grange_Hill_2SID.sid": {1: 125},
    "MUSICIANS/Y/Yaruni/Lethal_Xcess_Menu_2SID.sid": {1: 125},
}


def _play(swm) -> None:
    player = SWMPlayer(swm)
    for _ in range(CALLS):
        player.play_frame()


def assert_refused(path: str, swm) -> bool:
    """For a refused HVSC ``path``: each refused song raises its issue's refusal and every
    other subtune plays; True. False for a tune every song of which binds."""
    songs = REFUSED.get(path)
    if songs is None:
        return False
    for n in range(swm.subtune_count):
        if n + 1 in songs:
            with pytest.raises((ValueError, SIDFormatError), match=REASONS[songs[n + 1]]):
                _play(swm.subtune(n))
        else:
            _play(swm.subtune(n))
    return True
