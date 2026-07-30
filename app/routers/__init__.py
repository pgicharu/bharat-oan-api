# Import all routers to make them available when importing from app.routers
# This allows main.py to do: from app.routers import chat, transcribe, suggestions, tts
from . import chat
from . import transcribe
# from . import suggestions  # Commented out: suggestion agent disabled
from . import tts
from . import health
from . import token
from . import telemetry
from . import bap_webhook
