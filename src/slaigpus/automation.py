"""Playwright glue for slaigpus — the piece future agents import.

Two ways to drive the site, and the choice matters:

``browser_context(...)``
    Playwright owns the browser.  Fully headless-capable, reproducible,
    right for scheduled/unattended jobs.

``attach_over_cdp(...)``
    You already ran ``slaigpus viewer <site> --cdp`` and logged in by hand; the
    script attaches to that live window.  Right for sites with SSO, MFA, or
    anything else you cannot automate away.

Configured sessions select direct access or a managed SSH SOCKS connection.
The lower-level helpers still accept an explicit
:class:`~slaigpus.tunnel.SSHTunnel`.

Install the extra first::

    pip install 'slaigpus[automation]'
    playwright install chromium
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Sequence

from .browser import NO_LOCAL_DNS
from .config import Site, load_config
from .network import NetworkConnection
from .tunnel import SSHTunnel

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Browser, BrowserContext, Playwright


def _require_playwright() -> None:
    try:
        import playwright  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "playwright is not installed — run:\n"
            "    pip install 'slaigpus[automation]'\n"
            "    playwright install chromium"
        ) from exc


def proxy_settings(tunnel: SSHTunnel) -> dict:
    """Playwright's ``proxy=`` argument for this tunnel."""
    return {"server": tunnel.socks_url}


def browser_context(
    playwright: "Playwright",
    tunnel: "SSHTunnel | NetworkConnection",
    profile_dir: Path,
    headless: bool = False,
    block_local_dns: bool = True,
    args: Sequence[str] = (),
    **context_kwargs: Any,
) -> "BrowserContext":
    """Launch a persistent Chromium context routed through *tunnel*.

    Persistent (rather than a fresh incognito context) so that cookies and
    local storage survive between runs.  Pass the same ``profile_dir`` that
    ``slaigpus viewer`` uses and the two share a login — but never run both at
    once, Chrome locks the profile.
    """
    _require_playwright()
    profile_dir = Path(profile_dir).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)

    chromium_args = list(args)
    uses_ssh = bool(getattr(tunnel, "socks_url", ""))
    if uses_ssh and block_local_dns:
        chromium_args.append(f"--host-resolver-rules={NO_LOCAL_DNS}")
    elif not uses_ssh:
        chromium_args.append("--no-proxy-server")

    launch_options = dict(
        user_data_dir=str(profile_dir),
        headless=headless,
        args=chromium_args,
        **context_kwargs,
    )
    if uses_ssh:
        launch_options["proxy"] = proxy_settings(tunnel)  # type: ignore[arg-type]
    return playwright.chromium.launch_persistent_context(**launch_options)


def attach_over_cdp(
    playwright: "Playwright",
    cdp_port: int = 9222,
    host: str = "127.0.0.1",
) -> "Browser":
    """Attach to a Chrome already started by ``slaigpus viewer --cdp``.

    The proxy is whatever that Chrome was launched with, so do not pass proxy
    settings here — they would be ignored.
    """
    _require_playwright()
    return playwright.chromium.connect_over_cdp(f"http://{host}:{cdp_port}")


class SiteSession:
    """Selected network path + browser context as a single context manager.

    The shortest path from a configured site to a Playwright page::

        with SiteSession("intranet", headless=True) as s:
            s.page.goto(s.site.url)
            print(s.page.title())
    """

    def __init__(
        self,
        site: "str | Site",
        headless: bool = False,
        config_path: Optional[Path] = None,
        probe: bool = True,
        **context_kwargs: Any,
    ) -> None:
        if isinstance(site, Site):
            self.site = site
        else:
            self.site = load_config(config_path).get(site)
        self.site.validate()

        self.headless = headless
        self.probe = probe
        self._context_kwargs = context_kwargs

        self.tunnel: Optional[NetworkConnection] = None
        self.context: Optional["BrowserContext"] = None
        self.page = None
        self._playwright = None

    def __enter__(self) -> "SiteSession":
        _require_playwright()
        from playwright.sync_api import sync_playwright

        self.tunnel = NetworkConnection(
            mode=self.site.mode,
            ssh_host=self.site.ssh_host,
            port=self.site.socks_port,
            ssh_args=self.site.ssh_args,
        ).start()

        try:
            if self.probe and self.site.probe_target:
                self.tunnel.probe(*self.site.probe_target)

            self._playwright = sync_playwright().start()
            self.context = browser_context(
                self._playwright,
                self.tunnel,
                profile_dir=self.site.resolved_profile_dir(),
                headless=self.headless,
                block_local_dns=self.site.block_local_dns,
                **self._context_kwargs,
            )
            self.page = (
                self.context.pages[0]
                if self.context.pages
                else self.context.new_page()
            )
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def close(self) -> None:
        # Reverse order of construction; each step guarded so one failure does
        # not strand the others.
        if self.context is not None:
            try:
                self.context.close()
            except Exception:  # noqa: BLE001 - best effort teardown
                pass
            self.context = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None
        if self.tunnel is not None:
            self.tunnel.stop()
            self.tunnel = None
        self.page = None
