"""PyInstaller's entry script: GranolaAutostart.exe runs the package's __main__, which handles every mode.

A script outside the package, so the build imports granola_autostart as a package.
"""

from granola_autostart.__main__ import main

raise SystemExit(main())
