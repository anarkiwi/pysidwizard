"""SID-Maker run on the 6502 reproduces the release ``.sid`` exports of the example modules.

The 1.97 tarball's ``sid-exports`` are the 1.8 release's files (byte-identical), saved
by SID-Maker 1.8, so that build must match them byte for byte; later builds differ
only in player code that plays the same.
"""

import re

import pytest
from pysidtracker import SidImage
from pysidtracker.disasm import image_after_init
from pysidtracker.oracle import EmuPlayer

from pysidwizard import playspec, sidmaker
from pysidwizard.errors import SWMFormatError
from pysidwizard.reader import parse_swm
from tests._swm_cache import multi_sid_path, sw197_export_path

EXPORTS = (
    "bronkosaurus",
    "c0z-50shades",
    "euphoria",
    "flashitback",
    "futuco",
    "kerekesintro",
    "nepzenek",
    "pimpmycomm",
    "rain8580",
)
HEADER = 0x7C
RELEASED = slice(0x56, 0x76)
CTIME = re.compile(rb"[A-Z][a-z]{2} [A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d \d{4}\0+")
TEXT_FIELDS = (0x16, 0x36, 0x56)
FRAMES = 300
APPVERSION = {"1.7": b"1.7", "1.8": b"1.8", "1.94": b"1.9", "1.97": b"1.9"}


def _swm(name: str) -> bytes:
    return sw197_export_path(f"native/examples/{name}.swm").read_bytes()


def _sid(name: str) -> bytes:
    return sw197_export_path(f"native/examples/sid-exports/{name}.sid").read_bytes()


def _terminated(sid: bytes) -> bytes:
    """The header with each text field cut at its first NUL (SetSIDt never clears past it)."""
    out = bytearray(sid[:HEADER])
    for at in TEXT_FIELDS:
        field = bytes(out[at : at + 32])
        end = field.find(0)
        if end >= 0:
            out[at + end : at + 32] = bytes(32 - end)
    return bytes(out)


def _grid(sid: bytes):
    image = SidImage.from_sid(sid)
    player = EmuPlayer(
        bytes(image.image), image.load, image.header.init_address, image.header.play_address
    )
    return player.render_grid(FRAMES)


def _view(sid: bytes) -> bytes:
    return bytes(image_after_init(SidImage.from_sid(sid)).window(0, 0x10000))


@pytest.mark.parametrize(
    "name",
    EXPORTS
    + (
        pytest.param(
            "vn-waiting",
            marks=pytest.mark.xfail(
                strict=True,
                reason="the release .sid was saved from an earlier revision of this module: 38 "
                "more bytes of module data, so every later pointer shifts",
            ),
        ),
    ),
)
def test_sid_maker_1_8_saves_the_release_export(name):
    """Load address, player and packed module are the release export's, byte for byte."""
    out, release = sidmaker.export(_swm(name), "1.8"), _sid(name)
    assert out[HEADER:] == release[HEADER:]
    assert _terminated(out) == _terminated(release)


@pytest.mark.parametrize(
    "before, name", [("nepzenek", "pimpmycomm"), ("bronkosaurus", "c0z-50shades")]
)
def test_one_session_leaves_the_previous_author_after_the_terminator(before, name):
    """The release header bytes past a NUL are the previous export's, as one session saves them."""
    maker = sidmaker.SidMaker("1.8")
    maker.export(_swm(before))
    assert maker.export(_swm(name)) == _sid(name)
    assert sidmaker.export(_swm(name), "1.8") != _sid(name)


@pytest.mark.parametrize("name", ("bronkosaurus", "euphoria", "kerekesintro"))
@pytest.mark.parametrize("version", ("1.94", "1.97"))
def test_later_sid_makers_play_as_the_release_export(version, name):
    """A newer player build binds the same playspec and plays the same registers."""
    out, release = sidmaker.export(_swm(name), version), _sid(name)
    assert out != release
    assert playspec.binding(_view(out)) == playspec.binding(_view(release))
    assert _grid(out) == _grid(release)


@pytest.mark.parametrize("version", sidmaker.VERSIONS)
def test_each_build_exports_with_its_own_player(version):
    """Each shipped SID-Maker carries its release's player, tracker id and header facts."""
    out, release = sidmaker.export(_swm("nepzenek"), version), _sid("nepzenek")
    image = sidmaker.tune(_swm("nepzenek"), version)
    assert b" SID-WIZARD " + APPVERSION[version] + b" " in bytes(image.image)
    assert out[:0x16] == release[:0x16]
    assert image.load == 0x1000


def test_echo_sid_maker_1_96_saves_echo_test_but_its_host_clock():
    """The 1.96 echo build saves echo-test.sid but the "released" text, an EMULATOR build's
    copy of the host's ``ctime()`` that SetSIDt reads from the fake KERNAL at ``$E009``.
    """
    release = _sid("echo-test")
    assert CTIME.fullmatch(release[RELEASED])
    out = sidmaker.export(_swm("echo-test"), "1.96", "6581", echo=True)
    assert out == release.replace(release[RELEASED], bytes(32))


def test_echo_sid_maker_1_97_changes_only_the_player():
    """1.97's echo export differs from 1.96's only between PLAYERADDR and MUSICDATA."""
    swm, maker = _swm("echo-test"), sidmaker.SidMaker("1.97", echo=True)
    new, old = maker.export(swm, "6581"), sidmaker.export(swm, "1.96", "6581", echo=True)
    lab = maker.labels
    moved = [
        lab["EXEstarter"] + i - HEADER - 2
        for i, (a, b) in enumerate(zip(new, old, strict=True))
        if a != b
    ]
    assert moved
    assert all(lab["PLAYERADDR"] <= addr < lab["MUSICDATA"] for addr in moved)


def test_swm_file_and_its_bytes_export_alike():
    """An :class:`SWMFile` exports as the module it serialises to."""
    data = _swm("kerekesintro")
    assert sidmaker.export(parse_swm(data)) == sidmaker.export(data)


def test_model_selects_the_header_sid_model():
    """``model`` writes SID-Maker's own ModelFlag bits into the header flags."""
    data = _swm("kerekesintro")
    assert sidmaker.export(data, model="6581")[0x77] & 0x30 == 0x10
    assert sidmaker.export(data, model="8580")[0x77] & 0x30 == 0x20


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"swm": b"SWM1"}, SWMFormatError),
        ({"swm": b"not a module"}, SWMFormatError),
        ({"version": "1.5"}, ValueError),
        ({"model": "6582"}, ValueError),
        ({"version": "1.8", "echo": True}, ValueError),
    ],
)
def test_rejects_what_sid_maker_cannot_export(kwargs, error):
    """Truncated or foreign modules, unknown releases and SID models raise."""
    with pytest.raises(error):
        sidmaker.export(**{"swm": _swm("kerekesintro"), **kwargs})


def test_rejects_a_driver_the_release_lacks():
    """A driver byte past the release's player types is refused, not run."""
    data = bytearray(_swm("kerekesintro"))
    data[2 + 19] = 0xFF
    with pytest.raises(SWMFormatError, match="driver type 255"):
        sidmaker.export(bytes(data))


def test_rejects_a_multi_sid_module():
    """SID-Maker's single-SID build exports SWM1 only."""
    with pytest.raises(SWMFormatError, match="multi-SID"):
        sidmaker.export(multi_sid_path("childhood.sws").read_bytes())
