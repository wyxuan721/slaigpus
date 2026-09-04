"""slaigpus — manage a SenseCore GPU server directly or through SSH SOCKS.

Typical library usage::

    from slaigpus import SSHTunnel

    with SSHTunnel("b-host") as t:
        print(t.socks_url)          # socks5://127.0.0.1:54321
        ...                         # drive Playwright / requests through it

Everything is cleaned up when the ``with`` block exits, including on Ctrl-C
or an unhandled exception.
"""

from .tunnel import SSHTunnel, TunnelError
from .network import NetworkConnection
from .browser import (
    ChromeArgumentError,
    ChromeError,
    ChromeNotFound,
    find_chrome,
    launch_chrome,
)
from .config import Config, Site, load_config
from .credentials import (
    CredentialStoreError,
    FileCredentialStore,
    SenseCoreCredentials,
    default_credentials_file,
)
from .cci import (
    AutoRenewControlStore,
    CCI_HARD_LIMIT_SECONDS,
    CCIError,
    CCIStatus,
    RenewalSupervisor,
    SenseCoreClient,
    TargetResolver,
)
from .dnat import (
    DNATClient,
    DNATCreatePlan,
    DNATCreateResult,
    DNATError,
    DNATSpec,
    EIP_RESOURCE_GROUP,
    EIP_SUBSCRIPTION,
    EIP_ZONE,
)

__all__ = [
    "SSHTunnel",
    "NetworkConnection",
    "TunnelError",
    "launch_chrome",
    "find_chrome",
    "ChromeError",
    "ChromeNotFound",
    "ChromeArgumentError",
    "Config",
    "Site",
    "load_config",
    "CredentialStoreError",
    "FileCredentialStore",
    "SenseCoreCredentials",
    "default_credentials_file",
    "CCIError",
    "CCIStatus",
    "CCI_HARD_LIMIT_SECONDS",
    "AutoRenewControlStore",
    "SenseCoreClient",
    "TargetResolver",
    "RenewalSupervisor",
    "DNATClient",
    "DNATCreatePlan",
    "DNATCreateResult",
    "DNATError",
    "DNATSpec",
    "EIP_RESOURCE_GROUP",
    "EIP_SUBSCRIPTION",
    "EIP_ZONE",
]

__version__ = "0.7.0"
