#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Touch-Seq: a touchscreen live-performance front end for the Carla plugin host — step sequencer (any
number of melody/drum tracks, 16 patterns per track, 8 scenes, bar-quantized switching), per-track plugin
chains and a master bus, live pads/keyboard/XY pad and performance faders, laid out as modular-synth style
modules.

The app itself is the `tseq` package beside this file (`tseq/model.py`, `engine.py`, `ui.py`, one module per
rack panel, `app.py` for startup). This file is the entry point: it flattens the package into one namespace
the way the single-file app used to have, so `import touchseq as ts` still sees everything, and starts it.
"""
from tseq.app import main

# The whole public surface in one place, as before the split into tseq/: panels, model, helpers and the Qt
# names that come with them. Scripts (and the tests) do `import touchseq as ts; ts.Engine`, etc.
from tseq.constants import *  # noqa: F401,F403
from tseq.music import *  # noqa: F401,F403
from tseq.model import *  # noqa: F401,F403
from tseq.engine import *  # noqa: F401,F403
from tseq.midiin import *  # noqa: F401,F403
from tseq.gestures import *  # noqa: F401,F403
from tseq.ui import *  # noqa: F401,F403
from tseq.sequencer import *  # noqa: F401,F403
from tseq.controls import *  # noqa: F401,F403
from tseq.host import *  # noqa: F401,F403
from tseq.performer import *  # noqa: F401,F403
from tseq.eq import *  # noqa: F401,F403
from tseq.patchbay import *  # noqa: F401,F403
from tseq.rack import *  # noqa: F401,F403
from tseq.dialogs import *  # noqa: F401,F403
from tseq.app import *  # noqa: F401,F403

__version__ = "0.9.0"

if __name__ == "__main__":
    main(__version__)
