"""Presence-only startup check; never sends a model request or prints a key."""
import sys
from app.agent.model_client import configuration, probe
from app.domain.errors import DomainError
from app.config import ROOT  # noqa: F401: load the local ignored configuration

if __name__ == "__main__":
    try:
        config = configuration()
        if '--probe' in sys.argv:
            probe()
            print('DeepSeek connection and tool calling verified.')
        else:
            print('DeepSeek configuration present: ' + config.model + ' (key hidden; not a connection test)')
    except DomainError as error:
        print(error.code + ': ' + error.message)
        raise SystemExit(1)
