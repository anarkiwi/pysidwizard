"""pysidwizard's renderer for the ``pysidtracker.survey`` whole-song HVSC oracle survey.

    PYTHONPATH=scripts python -m pysidtracker.survey run $HVSC tunes.csv \
        --work .hvsc-survey --render oracle_hvsc:render
"""

import numpy as np
from pysidtracker.cadence import playroutine_cadence
from pysidtracker.registers import PAL_CYCLES_PER_FRAME

from pysidwizard import SWMPlayer, parse_sid

PLAYER = "sidwizard"


def render(data, song, nframes):
    """Render 1-based ``song`` of a SID-Wizard ``.sid`` and the build options it decoded."""
    swm = parse_sid(data)
    cadence = playroutine_cadence(data, subtune=song - 1)
    info = {
        "driver": swm.driver_type,
        "frame_speed": swm.frame_speed,
        "rsid": data[:4] == b"RSID",
        "trigger": cadence.source and cadence.source.value,
        "speed": round(PAL_CYCLES_PER_FRAME / cadence.cycles_per_call),
        "dynamic": cadence.dynamic,
    }
    grid = SWMPlayer(swm.subtune(song - 1)).render_grid(nframes)
    return np.asarray(grid, np.int16), info
