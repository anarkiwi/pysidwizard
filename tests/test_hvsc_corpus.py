"""Real-HVSC corpus tests for the SID-Wizard ``.sid`` reader.

The tune list is enumerated deterministically with ``sidid`` (see
:mod:`tests._hvsc_corpus` for the method) and baked in as **paths only** — no
copyrighted tune bytes live in the repo. Every test here skips cleanly when the
local HVSC tree is absent and runs for real against ``$HVSC`` (default
``/scratch/preframr/hvsc/C64Music``) when present.

The positive sample asserts, per tune, that :func:`parse_sid` / :func:`read_sid`
succeed, that :meth:`SidWizardSidParser.detect` classifies the tune as
``DIRECT`` (its ``SWM1``/``SWP1`` anchor is found statically, no init run), and
that the recovered model plays. The ``EXCLUDED_*`` tests assert each
out-of-scope class is rejected with a clear, specific error rather than a silent
mis-parse.
"""

from __future__ import annotations

import pysidtracker
import pytest

from pysidwizard import (
    PlayPattern,
    SIDFormatError,
    SidWizardSidParser,
    SWMPlayer,
    is_sidwizard_sid,
    parse_psid_header,
    parse_sid,
    read_sid,
)
from tests._hvsc_corpus import (
    EXCLUDED_MULTI_SID,
    EXCLUDED_UNRESOLVED,
    SAMPLE,
)
from tests._hvsc_fetch import resolve


def _path(rel: str):
    # Local $HVSC tree when present, else fetched from the HVSC mirror into the
    # gitignored cache. Skips this one tune only if genuinely unreachable, so
    # the corpus runs for real in CI (mirror reachable) instead of skipping.
    path = resolve(rel)
    if path is None:
        pytest.skip(f"tune unavailable (local HVSC or mirror): {rel}")
    return path


def _read(rel: str) -> bytes:
    return _path(rel).read_bytes()


@pytest.mark.parametrize("rel", SAMPLE)
def test_sample_parses_detects_direct_and_plays(rel):
    path = _path(rel)
    data = path.read_bytes()
    assert is_sidwizard_sid(data) is True

    # detect() must classify the direct-load export as DIRECT without emulating
    # init (its SWM1/SWP1 anchor is found statically in the loaded image).
    detection = SidWizardSidParser().detect(data)
    assert detection.kind is pysidtracker.PlayroutineKind.DIRECT
    assert detection.ran_init is False
    assert detection.anchor

    swm = parse_sid(data)
    # read_sid(path) is the same code path from disk.
    assert read_sid(str(path)).subtune_count == swm.subtune_count

    # Structural sanity: every orderlist pattern reference resolves and every
    # decoded row note is a legal pitch/effect byte.
    max_ref = max(
        (c.pattern for s in swm.sequences for c in s if isinstance(c, PlayPattern)),
        default=0,
    )
    assert max_ref <= len(swm.patterns)
    for pat in swm.patterns:
        for row in pat.rows:
            assert row.note is None or row.note <= 0x7F

    player = SWMPlayer(swm)
    for _ in range(300):
        player.play_frame()

    if swm.subtune_count > 1:
        for n in range(swm.subtune_count):
            sub_player = SWMPlayer(swm.subtune(n))
            for _ in range(120):
                sub_player.play_frame()


def test_sample_is_representative():
    # Guard against the deterministic sample silently degrading to a single
    # trivial shape: it must still span RSID, packed (SWP) and multi-subtune.
    rsid = swp = multisubtune = 0
    for rel in SAMPLE:
        data = _read(rel)
        if data[:4] == b"RSID":
            rsid += 1
        if data.find(b"SWP1", parse_psid_header(data).data_start) >= 0:
            swp += 1
        if parse_sid(data).subtune_count > 1:
            multisubtune += 1
    assert rsid >= 1, "sample lost its RSID coverage"
    assert swp >= 1, "sample lost its packed (SWP) coverage"
    assert multisubtune >= 5, "sample lost its multi-subtune coverage"


@pytest.mark.parametrize("rel", EXCLUDED_MULTI_SID)
def test_excluded_multi_sid_rejected(rel):
    # Multi-SID (2-SID/3-SID) is out of scope: the player and model are
    # single-SID only, so it must reject rather than misrepresent one SID.
    data = _read(rel)
    assert is_sidwizard_sid(data) is False
    with pytest.raises(SIDFormatError, match="multi-SID"):
        parse_sid(data)


@pytest.mark.parametrize("rel", EXCLUDED_UNRESOLVED)
def test_excluded_unresolved_rejected(rel):
    # Recognised as SID-Wizard (an SWM1 magic or the player-code signature is
    # present, so the static recogniser anchors and detect() reports DIRECT),
    # but no coherent, playable layout resolves — a stale/template header or an
    # export whose tune data is not materialised statically. Neither the in-place
    # end-probe nor the relocation-invariant code scan resolves it, so parse_sid
    # must reject with a clear error rather than silently mis-parse.
    data = _read(rel)
    assert is_sidwizard_sid(data) is True
    detection = SidWizardSidParser().detect(data)
    assert detection.kind is pysidtracker.PlayroutineKind.DIRECT
    with pytest.raises(SIDFormatError):
        parse_sid(data)
