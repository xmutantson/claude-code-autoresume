#!/usr/bin/env pythonw
"""Alternative double-click launcher for the autoresume status window.

A .pyw file is associated with pythonw.exe on a standard Windows Python install,
so double-clicking it starts WITHOUT a console window -- only the Tkinter status
window appears. (autoresume-gui.vbs is the primary launcher and does not depend
on the .pyw file association; use this one if you prefer .pyw.)
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from autoresume import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["watch", "--gui"]))
