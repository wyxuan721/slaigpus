"""Launch a persistent slaigpus Chrome instance wired to the tunnel.

Design notes
------------
* A dedicated ``--user-data-dir`` means this Chrome is a *separate process*
  from your everyday browser.  Proxy settings, extensions and cookies do not
  leak either way, and quitting it does not touch your normal windows.
* The profile directory is persistent (not a temp dir) so that logins survive
  between runs — an agent that re-authenticates every time is fragile.
* ``--host-resolver-rules`` stops Chrome from doing local DNS lookups, which
  is what leaks internal hostnames to your ISP's resolver and, more
  practically, what makes intranet names fail to resolve at all.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence


class ChromeError(RuntimeError):
    """A managed Chrome could not be launched safely."""


class ChromeNotFound(ChromeError):
    """No Chromium-family browser could be located."""


class ChromeArgumentError(ChromeError):
    """A user-supplied flag conflicts with slaigpus's ownership/security flags."""


# Ordered by preference.  macOS layout, with ~/Applications fallbacks for
# per-user installs.
CHROME_CANDIDATES: List[str] = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "~/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    # Common system-wide Linux installations used by a headless controller.
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/opt/google/chrome/chrome",
    "/usr/lib/chromium/chromium",
]

# Rationale for each flag lives next to it; please keep it that way.
BASE_ARGS: List[str] = [
    "--no-first-run",               # skip the welcome flow on a fresh profile
    "--no-default-browser-check",   # never ask to become the default browser
    "--disable-features=ChromeWhatsNewUI",
    "--new-window",
]

# Chrome resolves *nothing* locally except loopback; every other name is handed
# to the SOCKS proxy, i.e. resolved on the jump host.
NO_LOCAL_DNS = "MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"


def find_chrome(explicit: str = "") -> str:
    """Return a usable Chrome executable path, or raise ``ChromeNotFound``."""
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        found = shutil.which(explicit)
        if found:
            return found
        raise ChromeNotFound(f"not an executable: {explicit}")

    env = os.environ.get("SLAIGPUS_CHROME")
    if env:
        return find_chrome(env)

    for candidate in CHROME_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)

    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        found = shutil.which(name)
        if found:
            return found

    raise ChromeNotFound(
        "could not find Chrome. Install it, or point at it with "
        "--chrome-binary / the SLAIGPUS_CHROME environment variable."
    )


def build_chrome_args(
    socks_port: int,
    profile_dir: Path,
    url: str = "",
    cdp_port: int = 0,
    enable_cdp: bool = False,
    block_local_dns: bool = True,
    extra_args: Sequence[str] = (),
    headless: bool = False,
    direct: bool = False,
) -> List[str]:
    args = [arg for arg in BASE_ARGS if not (headless and arg == "--new-window")]
    if headless:
        args.append("--headless=new")
    protected = {
        "--user-data-dir",
        "--proxy-server",
        "--proxy-bypass-list",
        "--no-proxy-server",
        "--proxy-pac-url",
        "--proxy-auto-detect",
        "--host-resolver-rules",
        "--remote-debugging-port",
        "--remote-debugging-address",
        "--remote-debugging-pipe",
        "--headless",
        # Both modes bypass the persistent profile that slaigpus owns.  In
        # particular, an automation login completed in either one would be
        # discarded at shutdown and the headless restart could never reuse it.
        "--incognito",
        "--guest",
    }
    cdp_enabled = bool(enable_cdp or cdp_port)
    for item in extra_args:
        flag = str(item).split("=", 1)[0]
        if flag in protected or (
            cdp_enabled and flag == "--remote-allow-origins"
        ) or (
            headless and flag == "--new-window"
        ):
            raise ChromeArgumentError(
                f"Chrome argument {flag} conflicts with slaigpus's managed browser settings"
            )
    # User customization comes first; the settings that define ownership,
    # proxy routing, DNS, and the CDP security boundary are appended last.
    args.extend(extra_args)
    args.append(f"--user-data-dir={profile_dir}")
    if direct:
        # Make "direct" deterministic instead of inheriting an operating-
        # system proxy or PAC setting behind the caller's back.
        args.append("--no-proxy-server")
    else:
        args.append(f"--proxy-server=socks5://127.0.0.1:{socks_port}")
        if block_local_dns:
            args.append(f"--host-resolver-rules={NO_LOCAL_DNS}")
    if cdp_enabled:
        if int(cdp_port) < 0 or int(cdp_port) > 65535:
            raise ChromeArgumentError("Chrome DevTools port must be between 0 and 65535")
        # CDP is full browser control.  Bind it explicitly to loopback and use
        # Chrome's port 0 mode for the automatic watcher, avoiding the
        # release-and-rebind race of selecting a free port in advance.
        # WebSocket clients suppress Origin, so a global
        # --remote-allow-origins=* exception is neither needed nor desirable.
        args.append(f"--remote-debugging-port={cdp_port}")
        args.append("--remote-debugging-address=127.0.0.1")
    if url:
        args.append(url)
    return args


def ensure_private_profile_dir(profile_dir: Path) -> Path:
    """Create a user-owned, non-symlink Chrome profile with mode ``0700``.

    Chrome writes ``DevToolsActivePort`` and persistent login material below
    this directory.  Those files may have ordinary file modes, so the profile
    directory itself is the local-account confidentiality boundary.
    """
    profile = Path(profile_dir).expanduser()
    try:
        mode = profile.lstat().st_mode
    except FileNotFoundError:
        try:
            profile.mkdir(parents=True, mode=0o700, exist_ok=False)
            mode = profile.lstat().st_mode
        except OSError as exc:
            raise ChromeError(f"could not create private Chrome profile: {profile}") from exc
    except OSError as exc:
        raise ChromeError(f"could not inspect Chrome profile: {profile}") from exc

    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ChromeError(f"Chrome profile must be a real directory: {profile}")
    try:
        details = profile.stat()
    except OSError as exc:
        raise ChromeError(f"could not inspect Chrome profile: {profile}") from exc
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and details.st_uid != getuid():
        raise ChromeError(f"Chrome profile is not owned by the current user: {profile}")
    try:
        profile.chmod(0o700)
    except OSError as exc:
        raise ChromeError(f"could not make Chrome profile private: {profile}") from exc
    return profile


def launch_chrome(
    socks_port: int,
    profile_dir: Path,
    url: str = "",
    cdp_port: int = 0,
    enable_cdp: bool = False,
    binary: str = "",
    block_local_dns: bool = True,
    extra_args: Sequence[str] = (),
    headless: bool = False,
    direct: bool = False,
) -> subprocess.Popen:
    """Start Chrome and return the process handle (not waited on).

    ``direct=True`` keeps the managed profile/CDP safety boundary but omits
    slaigpus's SOCKS and remote-DNS flags.  Proxy-related user flags remain
    protected in both modes so a caller cannot silently replace the selected
    network policy through ``extra_args``.
    """
    executable = find_chrome(binary)
    profile_dir = ensure_private_profile_dir(profile_dir)

    argv = [executable] + build_chrome_args(
        socks_port=socks_port,
        profile_dir=profile_dir,
        url=url,
        cdp_port=cdp_port,
        enable_cdp=enable_cdp,
        block_local_dns=block_local_dns,
        extra_args=extra_args,
        headless=headless,
        direct=direct,
    )
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
