"""Loads a local .env file (KEY=value per line) so the app runs on a laptop without exporting variables.
Real environment variables always win, so hosted deployments are unaffected."""
import os

_env = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_env):
    with open(_env, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
