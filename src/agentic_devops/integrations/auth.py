"""Credential resolution. No secrets are stored in code or config files.

Azure DevOps, in order of preference:
  1. ``SYSTEM_ACCESSTOKEN``  - the pipeline's job access token (Build Service identity)
  2. Entra ID token via azure-identity (workload identity federation / managed identity / az login)
  3. ``AZURE_DEVOPS_PAT``    - personal access token, for local development only

Azure Databricks, in order of preference:
  1. ``DATABRICKS_TOKEN_<ENV>`` / ``DATABRICKS_TOKEN`` (local development only)
  2. Entra ID token via azure-identity (service principal behind an Azure DevOps service connection)
"""

from __future__ import annotations

import base64
import os
import threading
import time
from collections.abc import Callable

ADO_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"  # Azure DevOps resource id
DATABRICKS_SCOPE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default"  # Azure Databricks resource id
COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"  # Microsoft Foundry


class EntraTokenProvider:
    """Caches an Entra ID access token and refreshes it 5 minutes before expiry."""

    def __init__(self, scope: str, credential=None):
        self.scope = scope
        self._credential = credential
        self._token: str | None = None
        self._expires_on = 0.0
        self._lock = threading.Lock()

    def _cred(self):
        if self._credential is None:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise RuntimeError(
                    "azure-identity is required for Entra ID auth: pip install 'agentic-devops[azure]'"
                ) from exc
            self._credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        return self._credential

    def token(self) -> str:
        with self._lock:
            if not self._token or time.time() > self._expires_on - 300:
                tok = self._cred().get_token(self.scope)
                self._token, self._expires_on = tok.token, float(tok.expires_on)
            return self._token

    def __call__(self) -> str:
        return self.token()


def bearer(token: str) -> Callable[[], str]:
    return lambda: f"Bearer {token}"


def basic_pat(pat: str) -> Callable[[], str]:
    encoded = base64.b64encode(f":{pat}".encode()).decode()
    return lambda: f"Basic {encoded}"


def ado_auth_from_env() -> Callable[[], str]:
    if os.environ.get("SYSTEM_ACCESSTOKEN"):
        return bearer(os.environ["SYSTEM_ACCESSTOKEN"])
    if os.environ.get("AZURE_DEVOPS_PAT"):
        return basic_pat(os.environ["AZURE_DEVOPS_PAT"])
    provider = EntraTokenProvider(ADO_SCOPE)
    return lambda: f"Bearer {provider.token()}"


def databricks_auth_from_env(environment: str) -> Callable[[], str]:
    token = os.environ.get(f"DATABRICKS_TOKEN_{environment.upper()}") or os.environ.get("DATABRICKS_TOKEN")
    if token:
        return bearer(token)
    provider = EntraTokenProvider(DATABRICKS_SCOPE)
    return lambda: f"Bearer {provider.token()}"
