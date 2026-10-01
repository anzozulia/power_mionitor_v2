"""App-registry entry point for the ``powermon`` app.

Models live in the feature packages, which are not Django apps. Django gives them the
``powermon`` app label because they sit inside this installed app, and importing them here
makes the registry load them. The single migration sequence is ``powermon/migrations/``.
"""

from powermon.engine.models import LocationState  # noqa: F401
from powermon.locations.models import Location  # noqa: F401
