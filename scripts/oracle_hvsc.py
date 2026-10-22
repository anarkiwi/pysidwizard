"""pysidwizard's renderer for the ``pysidtracker.survey`` whole-song HVSC oracle survey.

    PYTHONPATH=scripts python -m pysidtracker.survey run $HVSC tunes.csv \
        --work .hvsc-survey --render oracle_hvsc:render
"""

import numpy as np
from pysidtracker.cadence import cadence_info

from pysidwizard import SWMPlayer, parse_sid_song

PLAYER = "sidwizard"


def render(data, song, nframes):
    """Render 1-based ``song`` of a SID-Wizard ``.sid`` and the build options it decoded."""
    swm = parse_sid_song(data, song)
    info = {"driver": swm.driver_type, "frame_speed": swm.frame_speed}
    info.update(cadence_info(data, song))
    grid = SWMPlayer(swm).render_grid(nframes)
    return np.asarray(grid, np.int16), info
