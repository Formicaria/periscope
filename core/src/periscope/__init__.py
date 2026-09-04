"""periscope — shared core for the periscope homelab Discord bot pack."""

from .bot import LabBot
from .config import Settings, env, env_bool, env_int, env_list, env_scope, load_dotenv_if_present
from .embeds import (
    Severity,
    human_bytes,
    human_duration,
    lab_embed,
    progress_bar,
    status_dot,
    truncate,
)
from .alerts import Alert, AlertRouter
from .statusboard import StatusBoard
from .views import ConfirmView, PaginatorView, RefreshView
from .http import HttpClient
from .webhook import WebhookServer
from .state import JsonState
from .logging import setup_logging
from .service import Setting, ServiceBot, ServiceSpec, SHARED_SETTINGS, settings_from_example
from .store import Store

__all__ = [
    "LabBot",
    "Settings",
    "env",
    "env_bool",
    "env_int",
    "env_list",
    "env_scope",
    "load_dotenv_if_present",
    "Severity",
    "human_bytes",
    "human_duration",
    "lab_embed",
    "progress_bar",
    "status_dot",
    "truncate",
    "Alert",
    "AlertRouter",
    "StatusBoard",
    "ConfirmView",
    "PaginatorView",
    "RefreshView",
    "HttpClient",
    "WebhookServer",
    "JsonState",
    "setup_logging",
    "Setting",
    "ServiceBot",
    "ServiceSpec",
    "SHARED_SETTINGS",
    "settings_from_example",
    "Store",
]

__version__ = "0.2.0"
