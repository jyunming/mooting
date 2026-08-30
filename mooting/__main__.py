"""`python -m mooting`, so the package can be launched without its console script.

`mooting serve --web` starts a session per browser connection, and the command
it launches has to work whatever is on PATH — an editable checkout, a virtualenv
that is not active, a Windows shim that has not been regenerated. Naming the
interpreter and the module sidesteps all of it.
"""

from .cli import main

raise SystemExit(main())
