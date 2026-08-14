from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

STATE_FILE = Path(".mstar_token_state.json")
TOKEN_ENV_NAME = "MD_AUTH_TOKEN"
DEFAULT_LAB_URL_TEMPLATE = "https://analyticslab.morningstar.com/user/{username}/lab?"


class AnalyticsLabLoginError(RuntimeError):
    """Raised when Analytics Lab cannot issue a usable token."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _next_utc_midnight(after: Optional[datetime] = None) -> datetime:
    base = after or _now_utc()
    tomorrow = base.date() + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def clean_token(raw: str) -> str:
    token = (raw or "").strip()
    token = re.sub(r"^Bearer\s+", "", token, flags=re.I)
    token = re.sub(r"^MD_AUTH_TOKEN\s*=\s*", "", token, flags=re.I)
    token = token.strip().strip("'\"{}").strip()
    token = "".join(token.split())
    return token


def _decode_base64url_json(part: str) -> Optional[Dict[str, Any]]:
    """Decode one JWT-style base64url section and require a JSON object."""
    try:
        padded = part + "=" * (-len(part) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = json.loads(decoded.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def is_analytics_lab_token_shape(token: str) -> bool:
    """Return True for a structurally valid compact JWT candidate.

    Analytics Lab currently copies a normal three-part JWT.  The signature can
    be base64url (common for RS256) or hexadecimal in some environments.  Token
    *identity* is deliberately not inferred from shape; the candidate is always
    verified with a live morningstar_data request before the dashboard reports
    Connected.
    """
    token = clean_token(token)
    parts = token.split(".")
    if len(parts) != 3:
        return False
    header, payload, signature = parts
    header_json = _decode_base64url_json(header)
    payload_json = _decode_base64url_json(payload)
    if header_json is None or payload_json is None or not signature:
        return False
    # Standard JWT signatures are base64url; a few Morningstar error messages
    # refer to hexadecimal signatures, so accept either and let live validation
    # be the source of truth.
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", signature) or re.fullmatch(r"[0-9A-Fa-f]+", signature))


def is_jwt_shape(token: str) -> bool:
    """Backward-compatible alias for the stricter Analytics Lab token check."""
    return is_analytics_lab_token_shape(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(clean_token(token).encode("utf-8")).hexdigest()[:16]


def decode_jwt_payload(token: str) -> Dict[str, Any]:
    token = clean_token(token)
    if not is_analytics_lab_token_shape(token):
        return {}
    return _decode_base64url_json(token.split(".")[1]) or {}


def token_expiry(token: str) -> Optional[datetime]:
    payload = decode_jwt_payload(token)
    exp = payload.get("exp")
    if exp is None:
        return None
    try:
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except Exception:
        return None


def token_invalid_reason(token: str, buffer_minutes: int = 10) -> Optional[str]:
    token = clean_token(token)
    if not token:
        return "missing"
    if not is_jwt_shape(token):
        return "malformed"
    exp = token_expiry(token)
    if exp is not None and _now_utc() >= exp - timedelta(minutes=buffer_minutes):
        return "expired"
    return None


def state_refresh_reason() -> Optional[str]:
    state = load_state()
    acquired_at = _parse_dt(state.get("token_acquired_at_utc"))
    if acquired_at and _now_utc() - acquired_at >= timedelta(hours=23, minutes=30):
        return "token_age_near_24_hours"

    hit_at = _parse_dt(state.get("last_daily_limit_hit_at"))
    reset_after = _parse_dt(state.get("daily_limit_reset_after_utc"))
    if hit_at and reset_after and _now_utc() >= reset_after:
        return "daily_limit_reset_has_passed"
    return None


def get_token_status() -> Dict[str, Any]:
    """Return safe token/quota metadata for the dashboard. Never returns the raw token."""
    token = clean_token(os.environ.get(TOKEN_ENV_NAME, ""))
    invalid = token_invalid_reason(token)
    refresh = state_refresh_reason()
    state = load_state()
    exp = token_expiry(token)
    reset_after = _parse_dt(state.get("daily_limit_reset_after_utc"))
    current_hash = token_hash(token) if token else ""
    state_hash = str(state.get("token_hash", ""))
    live_validated_at = str(state.get("live_validated_at_utc", ""))
    live_validated = bool(live_validated_at and current_hash and current_hash == state_hash)
    quota_limited = bool(state.get("token_validation_quota_limited", False))
    valid = bool(token) and invalid is None and refresh is None and live_validated
    reason = invalid or refresh or ("not_live_validated" if not live_validated else ("quota_limited" if quota_limited else "ready"))
    return {
        "valid": valid,
        "reason": reason,
        "expires_at_utc": _iso(exp) if exp else str(state.get("token_expires_at_utc", "")),
        "acquired_at_utc": str(state.get("token_acquired_at_utc", "")),
        "live_validated_at_utc": live_validated_at,
        "quota_limited": quota_limited,
        "daily_limit_hit_at_utc": str(state.get("last_daily_limit_hit_at", "")),
        "daily_limit_reset_after_utc": _iso(reset_after) if reset_after else "",
        "token_hash": state_hash,
    }


def remember_token(token: str, *, quota_limited: bool = False) -> None:
    token = clean_token(token)
    state = load_state()
    exp = token_expiry(token)
    state["token_hash"] = token_hash(token)
    state["token_acquired_at_utc"] = _iso(_now_utc())
    state["token_expires_at_utc"] = _iso(exp) if exp else ""
    state["live_validated_at_utc"] = _iso(_now_utc())
    state["token_validation_quota_limited"] = bool(quota_limited)
    state["last_daily_limit_hit_at"] = ""
    state["daily_limit_reset_after_utc"] = ""
    save_state(state)


def invalidate_token(reason: str = "rejected") -> None:
    """Clear a rejected candidate so the dashboard cannot keep showing Connected."""
    os.environ.pop(TOKEN_ENV_NAME, None)
    state = load_state()
    state["token_hash"] = ""
    state["token_acquired_at_utc"] = ""
    state["token_expires_at_utc"] = ""
    state["live_validated_at_utc"] = ""
    state["token_validation_quota_limited"] = False
    state["last_token_rejection_reason"] = str(reason)
    state["last_token_rejection_at_utc"] = _iso(_now_utc())
    save_state(state)


def mark_daily_limit_exceeded() -> None:
    state = load_state()
    hit_at = _now_utc()
    state["last_daily_limit_hit_at"] = _iso(hit_at)
    state["daily_limit_reset_after_utc"] = _iso(_next_utc_midnight(hit_at))
    save_state(state)


def _system_clipboard_text() -> str:
    """Read the OS clipboard on macOS or Linux/X11."""
    if shutil.which("pbpaste"):
        try:
            result = subprocess.run(
                ["pbpaste"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.stdout:
                return result.stdout
        except Exception:
            pass

    if shutil.which("xclip") and os.environ.get("DISPLAY"):
        try:
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.stdout:
                return result.stdout
        except Exception:
            pass

    return ""


def _clear_system_clipboard() -> None:
    """Clear stale clipboard contents before Analytics Lab copies a token."""
    if shutil.which("pbcopy"):
        try:
            subprocess.run(
                ["pbcopy"],
                input="",
                text=True,
                check=False,
                timeout=3,
            )
        except Exception:
            pass

    if shutil.which("xclip") and os.environ.get("DISPLAY"):
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-i"],
                input="",
                text=True,
                check=False,
                timeout=3,
            )
        except Exception:
            pass


def token_from_clipboard() -> str:
    return clean_token(_system_clipboard_text())


def _ensure_virtual_display() -> Optional[subprocess.Popen]:
    """Start Xvfb so hosted Linux can run normal headed Chromium."""
    if os.environ.get("DISPLAY"):
        return None

    xvfb = shutil.which("Xvfb")
    if not xvfb:
        raise AnalyticsLabLoginError(
            "Hosted browser needs Xvfb, but Xvfb is not installed. "
            "Add xvfb and xclip to packages.txt."
        )

    display = os.environ.get("MSTAR_XVFB_DISPLAY", ":99")
    proc = subprocess.Popen(
        [
            xvfb,
            display,
            "-screen",
            "0",
            "1440x1000x24",
            "-ac",
            "-nolisten",
            "tcp",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = display
    time.sleep(0.8)
    if proc.poll() is not None:
        raise AnalyticsLabLoginError("Could not start the hosted virtual display.")
    return proc


def _prompt_username_password() -> tuple[str, str]:
    username = os.environ.get("MSTAR_USERNAME", "").strip()
    password = os.environ.get("MSTAR_PASSWORD", "").strip()
    if not username:
        username = input("Morningstar username/email: ").strip()
    if not password:
        password = getpass.getpass("Morningstar password: ").strip()
    return username, password


def _try_configured_token_endpoint(username: str, password: str) -> str:
    """
    Optional hook if Morningstar Support provides a documented Analytics Lab token endpoint.

    Set MSTAR_ANALYTICS_TOKEN_ENDPOINT to use this. We deliberately do not assume
    /token/oauth because Direct Web Services tokens are not interchangeable with
    Analytics Lab MD_AUTH_TOKEN tokens used by morningstar_data.
    """
    endpoint = os.environ.get("MSTAR_ANALYTICS_TOKEN_ENDPOINT", "").strip()
    if not endpoint:
        return ""
    try:
        import requests  # type: ignore

        response = requests.post(endpoint, json={"username": username, "password": password}, timeout=60)
        response.raise_for_status()
        data = response.json()
        for key in ["token", "access_token", "authToken", "md_auth_token", "MD_AUTH_TOKEN"]:
            token = clean_token(str(data.get(key, "")))
            if is_jwt_shape(token):
                return token
    except Exception:
        return ""
    return ""


def _fill_first_visible(page: Any, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                loc.fill(value)
                return True
        except Exception:
            pass
    return False


def _click_first_visible_text(page: Any, labels: list[str], timeout_ms: int = 1500) -> bool:
    for label in labels:
        try:
            loc = page.get_by_text(label, exact=False).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=timeout_ms)
                return True
        except Exception:
            pass
    return False


def _open_analytics_lab_token_menu(page: Any) -> bool:
    """Open JupyterLab's Analytics Lab menu so the token command becomes visible."""
    opened = False
    # Morningstar Analytics Lab is a JupyterLab extension. The copy-token command
    # is normally hidden under the top-level "Analytics Lab" menu.
    menu_candidates = [
        lambda: page.get_by_text("Analytics Lab", exact=True).last,
        lambda: page.get_by_role("menuitem", name="Analytics Lab").last,
        lambda: page.locator("#jp-MainMenu").get_by_text("Analytics Lab", exact=True).last,
    ]
    for candidate in menu_candidates:
        try:
            loc = candidate()
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=1500)
                page.wait_for_timeout(250)
                opened = True
                break
        except Exception:
            pass
    return opened


def _click_copy_authentication_token(page: Any) -> bool:
    """Click the copy-token command after the Analytics Lab menu is open."""
    selectors = [
        lambda: page.get_by_text("Copy Authentication Token", exact=False).last,
        lambda: page.get_by_role("menuitem", name=re.compile(r"Copy Authentication Token", re.I)).last,
        lambda: page.locator("[role='menuitem']").filter(has_text=re.compile(r"Authentication Token", re.I)).last,
        lambda: page.locator("text=/Copy.*Authentication.*Token/i").last,
    ]
    for candidate in selectors:
        try:
            loc = candidate()
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=2000)
                page.wait_for_timeout(600)
                return True
        except Exception:
            pass
    return False


def _read_browser_clipboard(page: Any, timeout_seconds: float = 5.0) -> str:
    """Read the value copied by the Analytics Lab menu command.

    We try three sources because browsers can expose clipboard data at slightly
    different times: page-level copy hooks, navigator.clipboard.readText(), and
    the operating-system clipboard.
    """
    deadline = time.time() + max(0.5, float(timeout_seconds))
    while time.time() < deadline:
        try:
            page.bring_to_front()
        except Exception:
            pass

        # Most reliable: capture exactly what the page passed to writeText().
        try:
            intercepted = clean_token(page.evaluate("window.__MSTAR_LAST_CLIPBOARD_TEXT || ''"))
            if is_jwt_shape(intercepted):
                return intercepted
        except Exception:
            pass

        try:
            browser_value = clean_token(page.evaluate("navigator.clipboard.readText()"))
            if is_jwt_shape(browser_value):
                return browser_value
        except Exception:
            pass

        system_value = token_from_clipboard()
        if is_jwt_shape(system_value):
            return system_value

        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    return ""


def _find_token_in_page(page: Any) -> str:
    """
    Look only in token-like form controls and require the Analytics Lab format.

    Never scan the whole page for an arbitrary JWT: identity-provider pages often
    expose unrelated SSO access tokens, which Morningstar correctly rejects.
    """
    selectors = [
        "input[name*='token' i]",
        "input[id*='token' i]",
        "textarea[name*='token' i]",
        "textarea[id*='token' i]",
        "input[aria-label*='token' i]",
        "textarea[aria-label*='token' i]",
    ]
    for selector in selectors:
        try:
            values = page.locator(selector).evaluate_all(
                "els => els.map(e => e.value || e.textContent || '')"
            )
            for value in values:
                token = clean_token(str(value))
                if is_analytics_lab_token_shape(token):
                    return token
        except Exception:
            pass
    return ""


def _try_playwright_analytics_lab(
    username: str,
    password: str,
    lab_url: str,
    *,
    headless: bool = False,
    timeout_seconds: int = 180,
    allow_terminal_fallback: bool = True,
) -> str:
    """
    Automate Analytics Lab login and click Copy Authentication Token.

    The browser remains visible by default so the user can complete MFA if the
    institution requires it. The token-copy click and token capture are automatic;
    no Terminal copy/paste is needed in dashboard mode.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return ""

    try:
        virtual_display_proc: Optional[subprocess.Popen] = None
        with sync_playwright() as p:
            # Hosted Linux has no physical monitor. Run normal headed Chromium
            # inside Xvfb rather than Playwright's true headless mode; this
            # preserves ordinary clipboard behavior used by Analytics Lab.
            browser_headless = bool(headless)
            if os.name != "nt" and not os.environ.get("DISPLAY"):
                virtual_display_proc = _ensure_virtual_display()
                browser_headless = False

            launch_kwargs: Dict[str, Any] = {"headless": browser_headless}
            system_chromium = (
                os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip()
                or shutil.which("chromium")
                or shutil.which("chromium-browser")
                or shutil.which("google-chrome")
            )
            if system_chromium:
                launch_kwargs["executable_path"] = system_chromium

            launch_kwargs["args"] = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1440,1000",
            ]
            print(
                f"[MSTAR AUTH] chromium={system_chromium or 'playwright bundled'} "
                f"headless={browser_headless} display={os.environ.get('DISPLAY', '')}"
            )
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context()
            try:
                context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://analyticslab.morningstar.com")
            except Exception:
                pass
            # Capture the exact value written by Analytics Lab's copy command.
            # This avoids relying solely on OS clipboard timing/permissions.
            try:
                context.add_init_script(
                    """
                    (() => {
                      const save = (value) => {
                        try {
                          const text = String(value || '').trim();
                          if (text) window.__MSTAR_LAST_CLIPBOARD_TEXT = text;
                        } catch (_) {}
                      };

                      const installClipboardHook = () => {
                        try {
                          if (!navigator.clipboard) return;
                          const proto = Object.getPrototypeOf(navigator.clipboard);
                          if (!proto || proto.__mstarWrappedWriteText) return;
                          const original = proto.writeText;
                          if (typeof original !== 'function') return;
                          proto.writeText = async function(value) {
                            save(value);
                            return original.call(this, value);
                          };
                          Object.defineProperty(proto, '__mstarWrappedWriteText', {value: true});
                        } catch (_) {}
                      };

                      const installExecCommandHook = () => {
                        try {
                          const proto = Document.prototype;
                          if (proto.__mstarWrappedExecCommand) return;
                          const original = proto.execCommand;
                          if (typeof original !== 'function') return;
                          proto.execCommand = function(command, ...args) {
                            if (String(command || '').toLowerCase() === 'copy') {
                              try {
                                const active = this.activeElement;
                                save(active && 'value' in active ? active.value : '');
                                save(this.getSelection ? this.getSelection().toString() : '');
                              } catch (_) {}
                            }
                            return original.call(this, command, ...args);
                          };
                          Object.defineProperty(proto, '__mstarWrappedExecCommand', {value: true});
                        } catch (_) {}
                      };

                      const installCopyEventHook = () => {
                        try {
                          document.addEventListener('copy', (event) => {
                            try {
                              const active = document.activeElement;
                              save(active && 'value' in active ? active.value : '');
                              save(window.getSelection ? window.getSelection().toString() : '');
                              if (event.clipboardData) {
                                save(event.clipboardData.getData('text/plain'));
                              }
                            } catch (_) {}
                          }, true);
                        } catch (_) {}
                      };

                      installClipboardHook();
                      installExecCommandHook();
                      installCopyEventHook();
                      document.addEventListener('DOMContentLoaded', () => {
                        installClipboardHook();
                        installExecCommandHook();
                      }, {once: true});
                    })();
                    """
                )
            except Exception:
                pass
            _clear_system_clipboard()
            page = context.new_page()
            page.goto(lab_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1200)

            username_filled = _fill_first_visible(
                page,
                [
                    "input[type='email']",
                    "input[name*='user' i]",
                    "input[id*='user' i]",
                    "input[name*='email' i]",
                    "input[id*='email' i]",
                    "input[type='text']",
                ],
                username,
            )
            if username_filled:
                _click_first_visible_text(page, ["Next", "Continue", "Sign in", "Log in", "Login"])
                page.wait_for_timeout(1200)

            password_filled = _fill_first_visible(
                page,
                ["input[type='password']", "input[name*='pass' i]", "input[id*='pass' i]"],
                password,
            )
            if password_filled:
                _click_first_visible_text(page, ["Sign in", "Log in", "Login", "Continue", "Submit"])
                try:
                    page.locator("input[type='password']").first.press("Enter")
                except Exception:
                    pass

            deadline = time.time() + max(30, int(timeout_seconds))
            launched = False
            while time.time() < deadline:
                # The launch action may navigate in-place or open a second tab.
                active_pages = [candidate for candidate in context.pages if not candidate.is_closed()]
                if active_pages:
                    page = active_pages[-1]

                # Some accounts show a launch step before JupyterLab opens.
                if not launched:
                    launched = _click_first_visible_text(
                        page,
                        ["Launch Analytics Lab", "Open Analytics Lab", "Launch Lab"],
                        timeout_ms=1000,
                    )
                    if launched:
                        page.wait_for_timeout(1000)

                # In JupyterLab the command is hidden under the top menu named
                # "Analytics Lab". Open that menu first, then click the command.
                menu_opened = _open_analytics_lab_token_menu(page)
                if _click_copy_authentication_token(page):
                    token = _read_browser_clipboard(page) or _find_token_in_page(page)
                    print(
                        f"[MSTAR AUTH] menu_opened={menu_opened} copy_clicked=True "
                        f"token_captured={bool(token)} url={page.url}"
                    )
                    if is_jwt_shape(token):
                        browser.close()
                        return token

                # Fallback for environments where the token is shown in a field.
                token = _find_token_in_page(page)
                if is_jwt_shape(token):
                    browser.close()
                    return token
                page.wait_for_timeout(750)

            if allow_terminal_fallback:
                print(
                    "Analytics Lab opened. Complete MFA/login in the browser if required. "
                    "The script will click Copy Authentication Token automatically when available."
                )
                input("Press Enter to retry token capture after browser login is complete...")
                token = _read_browser_clipboard(page) or _find_token_in_page(page)
                browser.close()
                return token if is_jwt_shape(token) else ""

            browser.close()
            return ""
    except Exception:
        return ""


def _manual_browser_clipboard_flow(username: str, lab_url: str) -> str:
    try:
        webbrowser.open(lab_url)
    except Exception:
        pass
    print("\nOpened Morningstar Analytics Lab if your system allows it.")
    print("Log in if needed, then click: Analytics Lab -> Copy Authentication Token.")
    input("Press Enter after the token is copied to clipboard...")
    token = token_from_clipboard()
    if is_jwt_shape(token):
        return token
    print("Clipboard did not contain a valid JWT token.")
    pasted = getpass.getpass("Paste MD_AUTH_TOKEN manually: ")
    return clean_token(pasted)


def _looks_like_daily_limit_error(exc: BaseException) -> bool:
    value = str(exc).lower()
    return any(marker in value for marker in [
        "daily query limit", "daily limit", "500000", "cells today", "exceeding your daily"
    ])


def _looks_like_auth_error(exc: BaseException) -> bool:
    value = str(exc).lower()
    return any(marker in value for marker in [
        "invalid jwt", "malformedjwt", "accessdenied", "forbidden", "unauthorized",
        "authentication", "authorization failed", "auth token", "token expired"
    ])


def validate_token_live(token: str) -> Dict[str, Any]:
    """Validate the captured token against one tiny Morningstar call before UI success."""
    token = clean_token(token)
    if not is_analytics_lab_token_shape(token):
        return {"valid": False, "quota_limited": False, "reason": "invalid_analytics_lab_format"}

    previous = os.environ.get(TOKEN_ENV_NAME)
    os.environ[TOKEN_ENV_NAME] = token
    try:
        import morningstar_data as md  # type: ignore

        md.direct.get_data_point_settings(data_point_ids=["MMR00"])
        return {"valid": True, "quota_limited": False, "reason": "accepted"}
    except Exception as exc:
        if _looks_like_daily_limit_error(exc):
            # Morningstar accepted authentication and then enforced the account quota.
            return {"valid": True, "quota_limited": True, "reason": "accepted_but_quota_limited"}
        # Some entitled accounts return 'There are no data points' after successful auth.
        if "there are no data points" in str(exc).lower():
            return {"valid": True, "quota_limited": False, "reason": "accepted_no_settings"}
        if _looks_like_auth_error(exc):
            return {"valid": False, "quota_limited": False, "reason": str(exc)}
        return {"valid": False, "quota_limited": False, "reason": f"validation_failed: {exc}"}
    finally:
        if previous is None:
            os.environ.pop(TOKEN_ENV_NAME, None)
        else:
            os.environ[TOKEN_ENV_NAME] = previous


def authenticate_with_credentials(
    username: str,
    password: str,
    *,
    use_browser: bool = True,
    headless: bool = False,
    timeout_seconds: int = 180,
) -> str:
    """
    Dashboard-safe authentication using supplied credentials.

    The raw credentials and token are never written to disk. If the institution
    requires MFA, the visible browser can be used to complete it; token capture
    remains automatic after MFA.
    """
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        raise AnalyticsLabLoginError("Enter the Morningstar username and password.")

    endpoint_token = _try_configured_token_endpoint(username, password)
    if token_invalid_reason(endpoint_token) is None:
        token = endpoint_token
    elif use_browser:
        lab_url = os.environ.get("MSTAR_ANALYTICS_LAB_URL", "").strip() or DEFAULT_LAB_URL_TEMPLATE.format(username=username)
        token = _try_playwright_analytics_lab(
            username,
            password,
            lab_url,
            headless=headless,
            timeout_seconds=timeout_seconds,
            allow_terminal_fallback=False,
        )
    else:
        token = ""

    invalid = token_invalid_reason(token)
    if invalid:
        invalidate_token(f"capture_{invalid}")
        raise AnalyticsLabLoginError(
            "Analytics Lab did not return its Copy Authentication Token value. "
            "The browser may have exposed an unrelated SSO token, login may require MFA, "
            "or Morningstar may have changed the page."
        )

    validation = validate_token_live(token)
    if not validation.get("valid"):
        invalidate_token(str(validation.get("reason", "Morningstar rejected token")))
        raise AnalyticsLabLoginError(
            "Morningstar rejected the captured value, so the dashboard did not save it. "
            "Reconnect after the Analytics Lab page is fully loaded."
        )

    os.environ[TOKEN_ENV_NAME] = clean_token(token)
    remember_token(token, quota_limited=bool(validation.get("quota_limited")))
    return clean_token(token)


def acquire_token(
    allow_prompt: bool = True,
    prefer_clipboard: bool = True,
    use_browser: bool = True,
    lab_url_template: str = DEFAULT_LAB_URL_TEMPLATE,
) -> str:
    if prefer_clipboard:
        token = token_from_clipboard()
        if token_invalid_reason(token) is None:
            return token

    if not allow_prompt:
        return ""

    username, password = _prompt_username_password()
    endpoint_token = _try_configured_token_endpoint(username, password)
    if token_invalid_reason(endpoint_token) is None:
        return endpoint_token

    lab_url = os.environ.get("MSTAR_ANALYTICS_LAB_URL", "").strip() or lab_url_template.format(username=username)
    if use_browser:
        token = _try_playwright_analytics_lab(
            username,
            password,
            lab_url,
            headless=False,
            timeout_seconds=180,
            allow_terminal_fallback=True,
        )
        if token_invalid_reason(token) is None:
            return token

    return _manual_browser_clipboard_flow(username, lab_url)


def ensure_md_auth_token(
    allow_prompt: bool = True,
    prefer_clipboard: bool = True,
    force_refresh: bool = False,
    use_browser: bool = True,
) -> str:
    existing = clean_token(os.environ.get(TOKEN_ENV_NAME, ""))
    invalid = token_invalid_reason(existing)
    refresh_reason = state_refresh_reason()

    if existing and not invalid and not refresh_reason and not force_refresh:
        print("Using existing MD_AUTH_TOKEN from environment.")
        return existing

    reason = "forced_refresh" if force_refresh else (invalid or refresh_reason or "missing")
    print(f"Fresh Morningstar Analytics Lab token required: {reason}")
    token = acquire_token(allow_prompt=allow_prompt, prefer_clipboard=prefer_clipboard, use_browser=use_browser)
    new_invalid = token_invalid_reason(token)
    if new_invalid:
        raise RuntimeError(
            "token expired or invalid. Sign in to Analytics Lab and obtain a fresh token, then rerun."
        )
    validation = validate_token_live(token)
    if not validation.get("valid"):
        invalidate_token(str(validation.get("reason", "Morningstar rejected token")))
        raise RuntimeError("token expired or invalid. Morningstar rejected the refreshed token.")
    os.environ[TOKEN_ENV_NAME] = token
    remember_token(token, quota_limited=bool(validation.get("quota_limited")))
    print("Analytics Lab token accepted for this run.")
    return token
