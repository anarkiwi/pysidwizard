"""Deterministic HVSC corpus fixtures for the SID-Wizard ``.sid`` reader.

These are **path lists only** — no copyrighted tune bytes are stored. The tunes
themselves live in the reader's local HVSC tree (``$HVSC`` /
``/scratch/preframr/hvsc/C64Music``); :mod:`tests.test_hvsc_corpus` gates the
corpus tests to skip cleanly when that tree is absent and run for real when it
is present.

Enumeration method (deterministic, reproducible): every ``.sid`` under HVSC was
classified with ``sidid`` (``SIDIDCFG=.../sidid.cfg sidid <HVSC>``) and the
``Hermit/SidWizard_V1.x`` matches taken. That group label also covers the
``(SidWizard_2SID)`` / ``(SidWizard_3SID)`` signatures, which are split out here
via the PSID/RSID header (multi-SID) because pysidwizard is single-SID only by
design. Of the 1100 SID-Wizard tunes found on the reference tree:

* ``941`` are single-SID and parse as direct-load exports
  (including ``7`` RSID, ``26`` packed ``SWP`` exports and
  ``64`` multi-subtune tunes), each satisfying the invariant that
  every orderlist pattern reference resolves. :data:`SAMPLE` is a deterministic,
  stratified subset (covering every driver type, every subtune-count bucket,
  RSID and SWP) capped so the corpus test stays fast under ``-n auto``.
* ``48`` are multi-SID (2-SID/3-SID) — rejected by design; the
  player and model are single-SID only (:data:`EXCLUDED_MULTI_SID`).
* ``68`` carry no static ``SWM1``/``SWP1`` anchor — the stripped /
  relocated player exports documented out of scope in
  ``docs/SID_READ_SCOPE.md`` §6; they stay ``UNKNOWN`` even after emulating init
  (:data:`EXCLUDED_NO_ANCHOR`).
* ``43`` carry an ``SWM1``/``SWP1`` magic whose embedded counts
  match no coherent in-place pointer-table geometry — a stale player-template
  header, a relocated export, or an orderlist that resolves to a pattern that
  does not exist. No absolute/relative/reloc-offset interpretation resolves
  them and the reader rejects them cleanly (:data:`EXCLUDED_UNRESOLVED`).

The ``EXCLUDED_*`` lists are deterministic subsets used to assert the reader
rejects each out-of-scope class with a clear, specific error (never a silent
mis-parse).
"""

from __future__ import annotations

# Stratified, deterministic sample of single-SID direct-load SID-Wizard tunes
# that MUST parse, detect as DIRECT, and play.
SAMPLE = [
    "DEMOS/0-9/35_Years.sid",
    "DEMOS/0-9/64C.sid",
    "DEMOS/0-9/64_Wishes_of_the_Catstar.sid",
    "DEMOS/0-9/8-Bit_Bard.sid",
    "DEMOS/A-F/BMP2MC_2_12f_Release_Intro.sid",
    "DEMOS/A-F/Bad_Bagel.sid",
    "DEMOS/A-F/Beware_of_the_Leopard.sid",
    "DEMOS/A-F/Breadtronic.sid",
    "DEMOS/A-F/Canned_Heat.sid",
    "DEMOS/A-F/Fallen_Down_Reprise.sid",
    "DEMOS/G-L/Groundead.sid",
    "DEMOS/G-L/Handshake_Malfunction.sid",
    "DEMOS/G-L/Jingle_Bells.sid",
    "DEMOS/G-L/Kids_Playing_15_in_1_Games.sid",
    "DEMOS/M-R/Midnight_Drive.sid",
    "DEMOS/M-R/Oua_Oua_Song.sid",
    "DEMOS/M-R/Proriad.sid",
    "DEMOS/S-Z/SID_Runner.sid",
    "DEMOS/S-Z/Spray_Paint.sid",
    "DEMOS/S-Z/Twenty_Years.sid",
    "DEMOS/S-Z/Unknown_Sound_2.sid",
    "GAMES/G-L/Ill_Savior.sid",
    "GAMES/G-L/Immensity_preview_2.sid",
    "GAMES/M-R/Mega_Chase.sid",
    "GAMES/M-R/PREX.sid",
    "GAMES/M-R/Pentagorat_II.sid",
    "MUSICIANS/A/AMB/Argonaut.sid",
    "MUSICIANS/A/AMB/C0ACID-19.sid",
    "MUSICIANS/A/Agemixer/Gate_Opening.sid",
    "MUSICIANS/A/Ant1/Samwise.sid",
    "MUSICIANS/A/Archmage/Party_Pilgrim.sid",
    "MUSICIANS/A/Arturo_Dente/Tiny_Invaders.sid",
    "MUSICIANS/A/Ash_9/Cloud_9.sid",
    "MUSICIANS/A/Ash_9/Krieger.sid",
    "MUSICIANS/A/Ash_9/Together_as_One.sid",
    "MUSICIANS/B/Barik/Broken_Bottle.sid",
    "MUSICIANS/B/Barik/Zooong.sid",
    "MUSICIANS/B/Batsman/Broder_Louie.sid",
    "MUSICIANS/B/Batsman/Happy_Birthday_Phreedh.sid",
    "MUSICIANS/B/Batsman/Karantan.sid",
    "MUSICIANS/B/Batsman/Petscii_Attack.sid",
    "MUSICIANS/B/Batsman/Sixty4.sid",
    "MUSICIANS/B/Batsman/Vintersagoland.sid",
    "MUSICIANS/C/C0zmo/50_Shades_of_Gradius.sid",
    "MUSICIANS/C/C0zmo/Archon_Reimagined.sid",
    "MUSICIANS/C/C0zmo/Ben_Daglish_Tribute.sid",
    "MUSICIANS/C/C0zmo/Elite_Remake.sid",
    "MUSICIANS/C/C0zmo/Last_Ninja_Remix-Remix.sid",
    "MUSICIANS/C/C0zmo/Lykia_Boss.sid",
    "MUSICIANS/C/C0zmo/Lykia_City.sid",
    "MUSICIANS/C/C0zmo/Lykia_City_Interior.sid",
    "MUSICIANS/C/C0zmo/Lykia_Endcave.sid",
    "MUSICIANS/C/C0zmo/Lykia_Mode7_Flight.sid",
    "MUSICIANS/C/C0zmo/Lykia_Tower.sid",
    "MUSICIANS/C/C0zmo/Performers_Demo_2018.sid",
    "MUSICIANS/C/C0zmo/R-Type_Amiga_Title_Remake.sid",
    "MUSICIANS/C/C0zmo/Times_of_Lore.sid",
    "MUSICIANS/C/C0zmo/Wizard_of_Wor.sid",
    "MUSICIANS/C/C0zmo/Zeitanomalie_5.sid",
    "MUSICIANS/C/Chabee/Flowery_Meadow.sid",
    "MUSICIANS/C/Comu/Mama_Killa.sid",
    "MUSICIANS/C/Crokocat/Stab.sid",
    "MUSICIANS/D/DAM/Burn.sid",
    "MUSICIANS/D/DAM/Dream_Piper.sid",
    "MUSICIANS/D/DAM/Must_Dash.sid",
    "MUSICIANS/D/DAM/Senara_Gaze.sid",
    "MUSICIANS/D/Da_Blondie/A_Biea_Zwa_Biea.sid",
    "MUSICIANS/D/Da_Blondie/Chill_n_Beer.sid",
    "MUSICIANS/D/Daglish_Ben/Japanese.sid",
    "MUSICIANS/D/Dave_Sidnify/Taming_the_Fire.sid",
    "MUSICIANS/D/Deetsay/1_2_3_C64.sid",
    "MUSICIANS/D/Deetsay/Cracked.sid",
    "MUSICIANS/D/Deetsay/Pings_of_Rower.sid",
    "MUSICIANS/D/Deetsay/You_Have_My_Arp.sid",
    "MUSICIANS/D/DommoCORE64/Indigo.sid",
    "MUSICIANS/E/Emarti/Ceddin_Deden.sid",
    "MUSICIANS/E/Emarti/Pentagram-Lions_in_a_Cage.sid",
    "MUSICIANS/F/Flipside/Skybreaker_21.sid",
    "MUSICIANS/F/Flipside/Skybreaker_24.sid",
    "MUSICIANS/G/Garvalf/Dreaming_Stone.sid",
    "MUSICIANS/G/Garvalf/Reflex.sid",
    "MUSICIANS/G/Goerp/Hells_Jingle.sid",
    "MUSICIANS/G/Goerp/Ready_to_Go.sid",
    "MUSICIANS/G/Gregfeel/8-Bit_Warrior.sid",
    "MUSICIANS/G/Gregfeel/AxelF-Shots_out_of_Nowhere.sid",
    "MUSICIANS/G/Gregfeel/Bad_Sector.sid",
    "MUSICIANS/G/Gregfeel/Check_the_Sound_loop.sid",
    "MUSICIANS/G/Gregfeel/End_of_Summer.sid",
    "MUSICIANS/G/Gregfeel/Game_Changer.sid",
    "MUSICIANS/G/Gregfeel/Holiday_Train_credits.sid",
    "MUSICIANS/G/Gregfeel/I_am_Blue.sid",
    "MUSICIANS/G/Gregfeel/Pure_Happiness.sid",
    "MUSICIANS/H/Haschpipan/Edison_Holocaust.sid",
    "MUSICIANS/H/Hermit/A_Nevem_Villany_Leo.sid",
    "MUSICIANS/H/Hermit/Crawl_Out_Through_the_Fallout.sid",
    "MUSICIANS/H/Hermit/Csillag_szazketto_kettoskereszt.sid",
    "MUSICIANS/H/Hermit/Emomyst.sid",
    "MUSICIANS/H/Hermit/Fallout_4_Main_Theme.sid",
    "MUSICIANS/H/Hermit/Funtempo.sid",
    "MUSICIANS/H/Hermit/Lirai_Onvallomas.sid",
    "MUSICIANS/H/Hermit/Magyar_Nepzenek.sid",
    "MUSICIANS/H/Hermit/Pimp_My_Commodore.sid",
    "MUSICIANS/H/Hermit/Stalker-Fighting_Unknown.sid",
    "MUSICIANS/H/Hermit/Waterman.sid",
    "MUSICIANS/J/Jab/Hermanni.sid",
    "MUSICIANS/J/Johey/Fallen_Chip.sid",
    "MUSICIANS/K/Kompositkrut/Amazing_Horse.sid",
    "MUSICIANS/K/Kompositkrut/Metal_Ascender.sid",
    "MUSICIANS/K/Kompositkrut/Wedding_Waltz.sid",
    "MUSICIANS/L/LukHash/Bootloader.sid",
    "MUSICIANS/M/Maak/Illuminate.sid",
    "MUSICIANS/M/Mac_TUGCS/Methodist.sid",
    "MUSICIANS/M/Mario64/From_Berlin_to_Paris_tune_4.sid",
    "MUSICIANS/M/Max_F3H/Cosmic_Ark.sid",
    "MUSICIANS/M/Max_F3H/Future_Shock_Remake.sid",
    "MUSICIANS/M/Max_F3H/Solitude.sid",
    "MUSICIANS/M/Misfit/Rodman_Jr_plus.sid",
    "MUSICIANS/M/Moellpauk/Oblique.sid",
    "MUSICIANS/N/NecroPolo/A_Trip_to_Normandy.sid",
    "MUSICIANS/N/NecroPolo/Choronzon.sid",
    "MUSICIANS/N/NecroPolo/Get_to_the_End.sid",
    "MUSICIANS/N/NecroPolo/MIDIchlorian.sid",
    "MUSICIANS/N/NecroPolo/Snowy.sid",
    "MUSICIANS/N/NecroPolo/Yellowstorm.sid",
    "MUSICIANS/N/Nightbeat/One_of_Those_Days.sid",
    "MUSICIANS/N/Nikitin_Oleg/Happy_Belka_2013.sid",
    "MUSICIANS/S/Spider_Jerusalem/Awakening.sid",
    "MUSICIANS/S/Spider_Jerusalem/Gruseldideldido.sid",
    "MUSICIANS/S/Spider_Jerusalem/Testballon.sid",
    "MUSICIANS/S/Stone_James/Death_in_Rome.sid",
]

# Multi-SID (2-SID/3-SID): rejected with a "multi-SID" error (out of scope).
EXCLUDED_MULTI_SID = [
    "DEMOS/A-F/Devils_ReSIDence_3SID.sid",
    "MUSICIANS/C/Chabee/Stickman_2SID.sid",
    "MUSICIANS/C/Chiummo_Gaetano/Enchanted_Forest_3SID.sid",
    "MUSICIANS/D/DAM/Ember_2SID.sid",
    "MUSICIANS/H/Hermit/E_G_Blues_2SID.sid",
    "MUSICIANS/M/M_O_T/Take_em_Out_stereo_2SID.sid",
    "MUSICIANS/S/SIDwave/Arrow_of_Time_2SID.sid",
    "MUSICIANS/S/SIDwave/Kitaros_Dream_2SID.sid",
    "MUSICIANS/S/Stone_James/A_Final_Hyperbase_2SID.sid",
    "MUSICIANS/T/Televicious/Eviltwin_2SID.sid",
    "MUSICIANS/T/Televicious/Ring-a-sync_2SID.sid",
    "MUSICIANS/V/Vincenzo/Quad_Core_bottom_part_2SID.sid",
]

# No static SWM1/SWP1 anchor: stripped/relocated exports (scope doc §6).
EXCLUDED_NO_ANCHOR = [
    "DEMOS/0-9/3_Days.sid",
    "MUSICIANS/B/Batsman/Crapman.sid",
    "MUSICIANS/C/C0zmo/Autumn_Skies.sid",
    "MUSICIANS/C/C0zmo/Concert_tune_5.sid",
    "MUSICIANS/C/C0zmo/Transforming.sid",
    "MUSICIANS/C/Comu/Leet_it_3.sid",
    "MUSICIANS/D/Deetsay/Poesoe.sid",
    "MUSICIANS/E/Enzoottobit/Disco_China.sid",
    "MUSICIANS/H/Hermit/RastArok.sid",
    "MUSICIANS/L/LukHash/Cyberiad_Theory.sid",
    "MUSICIANS/L/LukHash/Perpetual_Motion.sid",
    "MUSICIANS/N/NecroPolo/Deeper_Underground.sid",
]

# Magic present but the pointer tables / orderlist do not resolve in place
# (stale/template header, relocated export, or dangling pattern reference):
# rejected with a clear SIDFormatError.
EXCLUDED_UNRESOLVED = [
    "DEMOS/M-R/Przepis_na_kopytka.sid",
    "GAMES/S-Z/Your_Fathers_Nightmare.sid",
    "MUSICIANS/A/Ant1/Techno_2.sid",
    "MUSICIANS/A/Arturo_Dente/Spider_Climber.sid",
    "MUSICIANS/B/Bod/Coprolalia.sid",
    "MUSICIANS/C/Chiummo_Gaetano/Dreamworld.sid",
    "MUSICIANS/D/DAM/Cadence.sid",
    "MUSICIANS/E/Emarti/Turn_Me_On.sid",
    "MUSICIANS/K/Kompositkrut/Out_of_Range.sid",
    "MUSICIANS/M/Moellpauk/Frantic4BHF_tune_2.sid",
    "MUSICIANS/N/NecroPolo/Excess_Intro.sid",
    "MUSICIANS/R/Razy/OHGJ_Anthology_2023_menu.sid",
]
