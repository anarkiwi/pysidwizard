"""Fetch + cache the SWM test tunes on demand, SHA-256 verified, never committed.

CSDB sources: the SID-Wizard 1.94 source tarball (four player-test tunes, one SWP) and
the SID-Wizard 1.7 smallpack (quickscore.swm). Any SHA-256 mismatch is a hard error.
Each file is one member of a pinned release archive, fetched by the shared
:func:`pysidtracker.testing.fetch_archive_member`. Test-only module, imported only
from ``tests/``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

from pysidtracker.testing import fetch_archive_member

TARBALL_URL = "https://csdb.dk/getinternalfile.php/276275/SID-Wizard-1.94-with-sources.tar.gz"
TARBALL_SHA256 = "544e36aff3fe14b7e4cf81a04c680a6883191a222754b2f0489e15349a89b559"

# Tune name -> (tarball member path, expected SHA-256 of the SWM bytes).
TUNE_MEMBERS: Dict[str, Tuple[str, str]] = {
    "flashitback": (
        "SID-Wizard-1.94/native/examples/flashitback.swm",
        "53fc4da79248737e28e04caa692c0794653b5db0f3ea0ee3d8917307d57cee60",
    ),
    "bronkosaurus": (
        "SID-Wizard-1.94/native/examples/bronkosaurus.swm",
        "96862c8fddf77cf3abbf2ff2010b761bcef4ea0968b5843a9408d1aec99e3591",
    ),
    "euphoria": (
        "SID-Wizard-1.94/native/examples/euphoria.swm",
        "5f7103f65288bbeb39200aa6f553f2e747742de96dd17edc0ff7675fd8264f7d",
    ),
    "rain8580": (
        "SID-Wizard-1.94/native/examples/rain8580.swm",
        "edbd461c0f1d92a18420589b86dcefb685e748e7be68b390681d68030fcafcaf",
    ),
}

TUNE_NAMES: Tuple[str, ...] = tuple(TUNE_MEMBERS.keys())

# Packed ``SWP`` music-data files from the tarball, keyed by cache file name.
SWP_FILES: Dict[str, Tuple[str, str]] = {
    "flashitback.swp": (
        "SID-Wizard-1.94/native/sources/SWP-slowdown-example/flashitback.swp",
        "ce7180b2dcb0902d760a40968354af35bf59c983a9ba34ba36a6cb360afd1dbe",
    ),
}

SWP_NAMES: Tuple[str, ...] = tuple(SWP_FILES.keys())

# quickscore.swm — all nine instruments store first-waveform byte $00 (the SWM1 $09-at-load default).
SW17_ZIP_URL = "https://csdb.dk/getinternalfile.php/131083/SID-Wizard-1.7-smallpack.zip"
SW17_ZIP_SHA256 = "c7fabb73aefa5a779d9ced7a661258c33e97ac4ee42bea22fa5ae8ee6e55167f"
QUICKSCORE_MEMBER = "SID-Wizard-1.7-smallpack/demotunes/didnt-fit-on-disk/quickscore.swm"
QUICKSCORE_SHA256 = "52cf7c8662aa297e7306b421f4966c5a5bacb79c5dd6dee79dca9a704d2329f0"

# issue #57: editor-saved modules that reference an instrument above the count, all in the 1.94 tarball.
OOB_INST_TUNE_MEMBERS: Dict[str, Tuple[str, str]] = {
    "vn-drdudas": (
        "SID-Wizard-1.94/native/examples/swm-more/vn-drdudas.swm",
        "b7af603d90a8ffdda02842138869e7c7734820688a8e356f4eb926b19fca2d9b",
    ),
    "vn-pimasz": (
        "SID-Wizard-1.94/native/examples/swm-more/vn-pimasz.swm",
        "4ad6a49b325ea4e4f250f75e9cfb4b1cafbc91748b0187ad7dc6d786ba309887",
    ),
    "np-sw19-emu": (
        "SID-Wizard-1.94/resources/examples/np-sw19-emu.swm",
        "fddde8c65f0f57154023bf4cd2218346db1e432af4a10a1427cd2ec8a4e18d33",
    ),
    "gurza-dream": (
        "SID-Wizard-1.94/resources/examples/gurza-dream.swm",
        "aafa3f8fffc40ce3d1104a97ebefee9eb81248bac525a314cc361a63486cc23b",
    ),
    "uraniumfever": (
        "SID-Wizard-1.94/resources/examples/uraniumfever.swm",
        "2581f052f7b13675d05c9b8237ab5b339d845044ea646b5b4e9c0caba629edf0",
    ),
}

OOB_INST_TUNE_NAMES: Tuple[str, ...] = tuple(OOB_INST_TUNE_MEMBERS.keys())

# issue #53: native multi-SID work files (SWMS/SWT/SWMQ), all in the 1.94 tarball.
MULTI_SID_MEMBERS: Dict[str, Tuple[str, str]] = {
    "childhood.sws": (
        "SID-Wizard-1.94/native/examples/childhood.sws",
        "2316115fb12447dce36fd0dfe5339327149936efdcfa3f453b3a846e65ce067a",
    ),
    "egblues.sws": (
        "SID-Wizard-1.94/native/examples/egblues.sws",
        "a6129d0f0c49e75c94cca5eb12cf3211e97c59e43cc2039f08b28a5aef06c2eb",
    ),
    "chb-stickman.sws": (
        "SID-Wizard-1.94/resources/examples/chb-stickman.sws",
        "17dfcba14fbe5321f155442dde57c61cb12af6b2d79c8f8455ddc3c67dafa06b",
    ),
    "newwave.swt": (
        "SID-Wizard-1.94/native/examples/newwave.swt",
        "be745aa47d80f4c90364ae81cb0a366ea0df6e5eb97c580067dc07d84330020c",
    ),
    "techno.swt": (
        "SID-Wizard-1.94/native/examples/techno.swt",
        "c28bb59c0b72f4a218bb3ab98a6e32f7a38ac3f4fc7b7720adead1bf7f661e00",
    ),
    "thizizda.swt": (
        "SID-Wizard-1.94/native/examples/thizizda.swt",
        "f82eb5e0868c19f0dcc443c8449daf6df262cd577f19eb8e9601d8cacab3544c",
    ),
    "ramos.swt": (
        "SID-Wizard-1.94/resources/examples/ramos.swt",
        "1fe367720d1926020854f16388f354c38c67fcff885634a829330a5bda0d5f73",
    ),
    "4sid-example.swq": (
        "SID-Wizard-1.94/resources/examples/4sid-example.swq",
        "87155adffb1928368718f6878b06bc1b0603ce18a944a1f5bb9df89fd16de2f7",
    ),
}

MULTI_SID_NAMES: Tuple[str, ...] = tuple(MULTI_SID_MEMBERS.keys())

# issue #56: the 1.97 release's native .swm and its .sid export (the per-
# (version, driver) FREQTBL image is derived from the .sid exports).
SW197_TARBALL_URL = (
    "https://csdb.dk/getinternalfile.php/281063/SID-Wizard-1.97-sources-examples.tar.gz"
)
SW197_TARBALL_SHA256 = "aeb265f4a778b2acb5aad13e8e98af882888292f749f29f4d18cbf49850e57bc"
_SW197_ROOT = "SID-Wizard-1.97-sources-examples"

SW197_MEMBERS: Dict[str, Tuple[str, str]] = {
    "bronkosaurus": (
        _SW197_ROOT + "/native/examples/bronkosaurus.swm",
        "96862c8fddf77cf3abbf2ff2010b761bcef4ea0968b5843a9408d1aec99e3591",
    ),
    "bronkosaurus.sid": (
        _SW197_ROOT + "/native/examples/sid-exports/bronkosaurus.sid",
        "0f65d71e4ef81a67f79f0cf808e7600f083cddeaa8c5dbc9ba905ec2c7ae16e6",
    ),
    "glacialblues": (
        _SW197_ROOT + "/native/examples/swm-more/extra/glacialblues.swm",
        "caf55c69b599d92dabea6084ffc9a824456f983c2a8a197e8aa926d169cd1c81",
    ),
    "glacialblues.sid": (
        _SW197_ROOT + "/native/examples/sid-exports/glacialblues.sid",
        "494e3ca6e937c9d1904bcb001cbcbb2d20a84d997679caa4a51dab7abebfb18e",
    ),
}

SW197_NAMES: Tuple[str, ...] = tuple(SW197_MEMBERS.keys())

# issue #59: SID-Maker exporter outputs (runnable .exe.prg, bare player+tune .prg) and their
# .sid / .swm twins, keyed by member path under the 1.97 tarball's root.
SW197_EXPORT_SHA256: Dict[str, str] = {
    "native/examples/prg-exports/bronkosaurus.exe.prg": "310a527de7f1193054af7c470714410f5cb0a3dcd332f48f31d68bf759de75af",
    "native/examples/sid-exports/bronkosaurus.sid": "0f65d71e4ef81a67f79f0cf808e7600f083cddeaa8c5dbc9ba905ec2c7ae16e6",
    "native/examples/prg-exports/c0z-50shades.exe.prg": "a637f31e6d7486817951ddc12ac894e907d4ed1420249ec23042bcdf513bd3a2",
    "native/examples/sid-exports/c0z-50shades.sid": "0b07d096c18a1126ee555806f6e7c983dfacf09c544fa89866c6a73369977cf7",
    "native/examples/prg-exports/cloudlessr.exe.prg": "894633327deabe308531461c4d997969cb1ba26c9eafb5a9a4186e02019b2589",
    "native/examples/sid-exports/cloudlessr.sid": "8a0b8e39bc21274e2ef6a3e9f143e5d65c2dc7fd9c00c323f34895a4b8f061fc",
    "native/examples/prg-exports/congabeat.exe.prg": "e0be59da6564b259328e834139a9fc552c01b948a9be01db89a37e84bb6e7002",
    "native/examples/sid-exports/congabeat.sid": "144e542115c1cbe988192e898902069e006f91421cbd3450384092673a1fe2d1",
    "native/examples/prg-exports/demo-example.exe.prg": "f9b79f1861a2d61396b39b9a88f4c1236fcd4f3d01661c96c2bd0b00408f749a",
    "native/examples/sid-exports/demo-example.sid": "8457724a9e93c529f7f1639d9dab10da99847d764dcd4051a9fccacf8e55718c",
    "native/examples/prg-exports/euphoria.exe.prg": "2a4bec3d54d5d292cec1e5845a2ca4359264234df38471a6e022fabd1c792bde",
    "native/examples/sid-exports/euphoria.sid": "92c0b644c7159b25c36c08a47c56ef38d98fd960d9e67635d26b50451b114c65",
    "native/examples/prg-exports/flashitback.exe.prg": "d1eaffd59508dff3ede7eb905efd7f185766b6e02fa124c6b75207a96d20473f",
    "native/examples/sid-exports/flashitback.sid": "05366df389629c6013b3b9a83c9f38c2b0edb1e2e1b7ba4f0c60d7077e313f50",
    "native/examples/prg-exports/futuco.exe.prg": "191463baa3182bb35144c4999c56d213793e8b14f4284e443eb27ae44d696dc2",
    "native/examples/sid-exports/futuco.sid": "3a6b91f3d70556c35d6669a3066f4ea09edd94b3e7553ef33b55e03641cf20f7",
    "native/examples/prg-exports/glacialblues.exe.prg": "9d97c291cb86e9b2247ac1812a0b68010e0943cf29a4d32f39ba4276109cfe5e",
    "native/examples/sid-exports/glacialblues.sid": "494e3ca6e937c9d1904bcb001cbcbb2d20a84d997679caa4a51dab7abebfb18e",
    "native/examples/prg-exports/kerekesintro.exe.prg": "494c6e98a43901e9314a5b05bb091d97768835b5b379861d9689c3036f13a3bd",
    "native/examples/sid-exports/kerekesintro.sid": "f0349c97d0eee7d5ad7ac26c3243cfeeee57455a3e7213acca65dbbba9087ca8",
    "native/examples/prg-exports/kicksamule.exe.prg": "8be41d4e0eb817745041fff9ffe29c87042ee4fd1fc1660d99427c9f655206bf",
    "native/examples/sid-exports/kicksamule.sid": "74024e71c15c287e553c2b857b20212cf9e49e2a42b7b4cc7a9fdf97c183e4a4",
    "native/examples/prg-exports/lenore.exe.prg": "96a5c8f396fe1c6cb031c7e9095ba7e1372d933b926bb22c1badc7264eaff393",
    "native/examples/sid-exports/lenore.sid": "e5da03524b3567d345d7817200a400fb83fdc9285a2ce69cdfb40129085cb406",
    "native/examples/prg-exports/nepzenek.exe.prg": "fa3427068ad3bc2db1b44fa365198d21140b57d5cf42ee0669f2f87cd4231a06",
    "native/examples/sid-exports/nepzenek.sid": "4ba1371ae072c27a760ebcd5390ff295a27934b774bc234b4344e57c3e13fc1a",
    "native/examples/prg-exports/oakyardmemo.exe.prg": "fed3bbdc53ae2bf0d206a7974a98c2e28218903dd33c7d6993cb516bdcda5c3f",
    "native/examples/sid-exports/oakyardmemo.sid": "62c4b5f1ff8729cf661e3c37d6783d7026a9d90f631abe12d72b1c518628d673",
    "native/examples/prg-exports/pimpmycomm.exe.prg": "566a542b2dc6cf1f2ce817ef5e78d047dc8a2f42b6202c59853175a1c5e0362a",
    "native/examples/sid-exports/pimpmycomm.sid": "6e2b83bb5e5f4f4d2347fff71b8c1b15bc9ac8796fb8a5c59a231a852f2a487b",
    "native/examples/prg-exports/rain8580.exe.prg": "3092faa5fb6b32b703129dd180741475151cdc21e8607b1ee52dee3fc6edbc99",
    "native/examples/sid-exports/rain8580.sid": "7882c170bd401167f7fbf77fb8a74472bdeb10bf884894413e9c7fa0c1b84931",
    "native/examples/prg-exports/versenyvagym.exe.prg": "7bdefad7b862186180a6fd401e2d4632bf6fc2d32769f09d5fda142a8f1886e4",
    "native/examples/sid-exports/versenyvagym.sid": "9df2754c7034b3193dff82d50ca0930fcaa3d55dba93157f1b5909c449140ecb",
    "native/examples/prg-exports/vn-waiting.exe.prg": "b5f5b98e1730a65263242f011aad23d060e83d45a7a122345666855e9bfe20db",
    "native/examples/sid-exports/vn-waiting.sid": "107f015f1355194feee88c03062d9a33a7206b2e98a444a0015d94ea13b5b107",
    "native/examples/prg-exports/childhood-2SID.exe.prg": "f7e70d46ff2e007042ab89fb016ae11bdaaabf49d09cfb2381377732be2a37b0",
    "native/examples/prg-exports/egblues-2SID.exe.prg": "3b724617aaca1d551136cd6c99635586a7c8e17c42dc63adce500fa48a5f8f7e",
    "native/examples/prg-exports/newwave-3SID.exe.prg": "69211a56dc068720b83b711bb49d3027701cec45bbcd3e76c6e9fe5c1e6ca3bc",
    "native/examples/prg-exports/ramos-3SID.exe.prg": "d1a865030ee2b9de47b74fa3ef1b9818654a143a081595284359ca7dda66c926",
    "native/examples/prg-exports/techno-3SID.exe.prg": "a728078a1f20a4bf794c138ee4504a385446aad3d7784aa01bc389093ceb050f",
    "native/examples/prg-exports/thizizda-3SID.exe.prg": "efd7399d11300663382075ff7085a22d627379f5de198175fc1cf721fb9c30f7",
    "native/examples/prg-exports/rastarok.exe.prg": "b7ca0c802bcb056c9eeeb53176d4feaaf324a5127bd002d63d099c5c56370a0e",
    "native/examples/prg-exports/echo-test.exe.prg": "a84b01e42604311460ad0d90229254bb56fd92fad550b6b4bf6f87ff7211ee35",
    "native/examples/sid-exports/echo-test.sid": "daed02398f0a13d6ddebb849b8afbf9d906b66bf583f2a29f747ecd658ecd1a5",
    "native/examples/echo-test.swm": "f66f8bc894dcbbbc0ec0ca5b82c79bce0b478e61bcc2940937dbabef42c53424",
    "native/sources/SFX-example/pimpnotersfx.prg": "2d3cbb6cb9711ef04c2785aed2cee3a6abcf82250f532897d8285c62d7a7a2b4",
    "native/examples/sid-exports/childhood-2SID.sid": (
        "055592975d70a19227ce9f63b5c4e58818cce6d4143bdff2cefa6a29c9470864"
    ),
    "native/examples/sid-exports/egblues-2SID.sid": (
        "e5eebaf82b10fd8673e0db281e811b67119272ca39e0a2caf83087846679dddd"
    ),
    "native/examples/sid-exports/newwave-3SID.sid": (
        "bfc0446cfc8607e227772d4120abb6d454ed1b8f4b2e889b8ef92624d9abea6c"
    ),
    "native/examples/sid-exports/ramos-3SID.sid": (
        "e21c290f463a8b5dc13ab4a6713133c828b08e5876182314f52844135f02a804"
    ),
    "native/examples/sid-exports/techno-3SID.sid": (
        "739ca33f66f557ac274c428c28797d965cde3ed1438bf775984a1081283b136b"
    ),
    "native/examples/sid-exports/thizizda-3SID.sid": (
        "e2941329b620cab298a4070ee710ff3f6854f4e87197b21343692be6d16c8292"
    ),
}

# issue #61: quickscore .P00 container + .prg twin from the SID-Wizard 1.4 release (csdb 115599).
SW14_ZIP_URL = "https://csdb.dk/getinternalfile.php/113613/SID-Wizard-1.4-with-source.zip"
SW14_ZIP_SHA256 = "6cd2e4c482f872a4e12a5ca2989039c6f3df079cab96a55562276a6973364ecc"
_SW14_ROOT = "SID-Wizard-1.4-with-source"

P00_PAIR_MEMBERS: Dict[str, Tuple[str, str]] = {
    "quickscore.swm.P00": (
        _SW14_ROOT + "/demotunes/quickscore.swm.P00",
        "fc4e260c8ab4666232e08fe97e8f0532eb240126629eaeaf89a27ba811a3fb08",
    ),
    "quickscore.swm.prg": (
        _SW14_ROOT + "/demotunes/quickscore.swm.prg",
        "52cf7c8662aa297e7306b421f4966c5a5bacb79c5dd6dee79dca9a704d2329f0",
    ),
}

P00_PAIR_NAMES: Tuple[str, ...] = tuple(P00_PAIR_MEMBERS.keys())

SW194 = (TARBALL_URL, TARBALL_SHA256)
SW197 = (SW197_TARBALL_URL, SW197_TARBALL_SHA256)
SW17 = (SW17_ZIP_URL, SW17_ZIP_SHA256)
SW14 = (SW14_ZIP_URL, SW14_ZIP_SHA256)


def release_member(release: Tuple[str, str], member: str, sha256: str, name: str) -> Path:
    """``member`` of the pinned ``(url, sha256)`` ``release`` archive, cached as ``name``."""
    return fetch_archive_member(*release, member, sha256, name=name)


def swm_path(tune: str) -> Path:
    """Path to the SID-Wizard 1.94 example ``{tune}.swm``."""
    return release_member(SW194, *TUNE_MEMBERS[tune], f"{tune}.swm")


def all_swm_paths() -> Dict[str, Path]:
    """Materialise all four tunes; return ``{tune: path}``."""
    return {tune: swm_path(tune) for tune in TUNE_NAMES}


def oob_inst_path(name: str) -> Path:
    """Issue #57 fixture (instrument ref above the count)."""
    return release_member(SW194, *OOB_INST_TUNE_MEMBERS[name], f"{name}.swm")


def multi_sid_path(name: str) -> Path:
    """Path to a native multi-SID work file (issue #53)."""
    return release_member(SW194, *MULTI_SID_MEMBERS[name], name)


def swp_path(name: str) -> Path:
    """Path to the packed ``SWP`` file ``name``."""
    return release_member(SW194, *SWP_FILES[name], name)


def quickscore_path() -> Path:
    """Path to quickscore.swm from the SID-Wizard 1.7 smallpack."""
    return release_member(SW17, QUICKSCORE_MEMBER, QUICKSCORE_SHA256, "quickscore.swm")


def sw197_path(name: str) -> Path:
    """Path to a 1.97 release member (``.swm`` or ``.sid``)."""
    return release_member(SW197, *SW197_MEMBERS[name], f"sw197-{name}")


def sw197_export_path(member: str) -> Path:
    """Path to a 1.97 exporter-output member (issue #59), e.g. ``native/sources/SFX-example/pimpnotersfx.prg``."""
    return release_member(
        SW197,
        f"{_SW197_ROOT}/{member}",
        SW197_EXPORT_SHA256[member],
        "sw197-" + member.replace("/", "-"),
    )


def p00_pair() -> Tuple[Path, Path]:
    """Return ``(p00, prg)`` — the 1.4 quickscore module wrapped and bare (issue #61)."""
    return tuple(release_member(SW14, *P00_PAIR_MEMBERS[n], n) for n in P00_PAIR_NAMES)
