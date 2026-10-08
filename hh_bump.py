"""Local HH resume bumping through the visible website, without HH API keys."""
from __future__ import annotations

import argparse
import errno
import getpass
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from hh_state import Store, atomic_json
from hh_ui import MODAL_TEST_JS, card_state, offered_locator, resume_keys

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
URL = "https://ekaterinburg.hh.ru/applicant/profile/me"
INTERVAL = 4 * 60 * 60 + 60
RUN_ID = uuid.uuid4().hex
STAGE = "startup"
CURRENT_STORE = None


class GlobalCycleError(RuntimeError):
    """Account, page, or dialog is unsafe for every resume in this cycle."""


class AmbiguousResumeError(RuntimeError):
    """A particular resume may have received a click; do not resend."""


def code_version():
    """Digest all production inputs once, before a browser or state is opened."""
    digest = hashlib.sha256()
    for name in ("hh_bump.py", "hh_state.py", "hh_ui.py", "requirements.txt",
                 "run-scheduled.ps1"):
        path = ROOT / name
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() if path.exists() else b"<missing>")
    return digest.hexdigest()


CODE_VERSION = code_version()


def certification_snapshot(page, expected_keys, *, require_cooldown=False):
    """Require the same HH account, card set, and known state before certifying."""
    if not is_profile_url(page.url):
        raise GlobalCycleError("Unexpected page during certification")
    keys = resume_keys(page)
    if not keys or set(keys) != set(expected_keys) or not CURRENT_STORE.account_matches(keys):
        raise GlobalCycleError("Resume list or account changed during certification")
    handle_dialogs(page, dismiss=False)
    states = {key: card_state(page, key)["state"] for key in keys}
    if "unknown" in states.values() or (require_cooldown and "offered" in states.values()):
        raise GlobalCycleError("Resume state is not certifiable")
    if (not is_profile_url(page.url) or set(resume_keys(page)) != set(keys)
            or not CURRENT_STORE.account_matches(keys)):
        raise GlobalCycleError("Page or account changed after certification read")
    handle_dialogs(page, dismiss=False)
    return states


def write_live_marker(validation, *, page, expected_keys, **fields):
    certification_snapshot(page, expected_keys, require_cooldown=validation == "run")
    if code_version() != CODE_VERSION:
        raise GlobalCycleError("Code changed during validation")
    atomic_json(STATE / "live-verified.json", {"time": time.time(),
                "code_version": CODE_VERSION, "validation": validation, **fields})


def set_stage(stage, **fields):
    global STAGE
    STAGE = stage
    STATE.mkdir(exist_ok=True)
    status = {"run_id": RUN_ID, "code_version": CODE_VERSION, "stage": stage,
              "heartbeat": datetime.now(timezone.utc).isoformat(), **fields}
    if CURRENT_STORE is not None:
        status["failure_count"] = CURRENT_STORE.state["failure_count"]
        status["next_due"] = CURRENT_STORE.due_at()
    atomic_json(STATE / "status.json", status)


def is_hh_url(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "hh.ru" or host.endswith(".hh.ru"))


def is_profile_url(url):
    """Accept HH applicant pages, including HH's regional-host redirects."""
    return is_hh_url(url) and urlsplit(url).path.startswith("/applicant/")


def dismiss_cookie_notice(page):
    """Use a recognized consent control only inside its cookie notice banner."""
    if not is_hh_url(page.url):
        return False
    labels = ("Понятно", "Принять", "Принять все", "Согласен")
    for attempt in range(2):
        if not is_hh_url(page.url):
            return False
        matches = []
        try:
            for label in labels:
                buttons = page.get_by_role("button", name=label, exact=True)
                for index in range(buttons.count()):
                    button = buttons.nth(index)
                    if not button.is_visible():
                        continue
                    handle = button.element_handle()
                    if handle is None:
                        continue
                    scoped_to_notice = handle.evaluate("""button => {
                        for (let node = button.parentElement; node && node !== document.body;
                             node = node.parentElement) {
                            const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                            const cookieNotice = /(?:cookie|куки|файл\\w*\\s+cookie)/i.test(text);
                            const buttons = node.querySelectorAll('button, [role="button"]');
                            if (cookieNotice && text.length <= 600 && buttons.length <= 3 &&
                                Array.from(buttons).includes(button)) return true;
                        }
                        return false;
                    }""")
                    if scoped_to_notice:
                        matches.append((handle, label))
        except PlaywrightError as exc:
            if "Element is not attached to the DOM" not in str(exc):
                raise
            matches = []
        if len(matches) == 1:
            handle, label = matches[0]
            if not is_hh_url(page.url):
                return False
            try:
                handle.click(timeout=2000)
            except PlaywrightError as exc:
                if "Element is not attached to the DOM" not in str(exc):
                    raise
                if attempt == 0:
                    page.wait_for_timeout(100)
                    continue
                return False
            record("cookie_notice_accepted" if label != "Понятно" else "cookie_notice_dismissed")
            return True
        if len(matches) > 1:
            record("cookie_notice_ambiguous", buttons=len(matches))
            return False
        if attempt == 0:
            page.wait_for_timeout(100)
    return False


def wait_for_cookie_notice(page, timeout=3):
    """Poll briefly at a safe page checkpoint for a delayed, explicitly scoped notice."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.is_closed():
            return False
        if dismiss_cookie_notice(page):
            return True
        page.wait_for_timeout(250)
    return False


def dismiss_hh_pro_promotion(page):
    """Close only one unambiguous hh PRO dialog through its close control."""
    title_locators = (
        page.get_by_text("Хотите быстрее получить приглашение?", exact=True),
        page.get_by_text(re.compile(r"^\s*(?:с[\s\u00a0]+)?hh[\s\u00a0]+PRO:\s*$", re.IGNORECASE)),
    )
    visible_titles = []
    for locator in title_locators:
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if candidate.is_visible():
                visible_titles.append(candidate)
    if not visible_titles:
        return False

    modals = []
    scoped_titles = []
    for title in visible_titles:
        title_handle = title.element_handle()
        modal_handle = title_handle.evaluate_handle("title => {" + MODAL_TEST_JS + """
            for (let node = title; node && node !== document.body; node = node.parentElement) {
                const normalize = value => value.replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
                const titleText = normalize(title.innerText || title.textContent || '');
                const text = normalize(node.innerText || '');
                const buttons = node.querySelectorAll('button, [role="button"]');
                const recognizedTitle = titleText.includes('Хотите быстрее получить приглашение?') ||
                    /^(?:с\\s+)?hh pro:$/i.test(titleText);
                if (recognizedTitle && isModal(node) && text.includes(titleText) &&
                    text.length <= 1200 && buttons.length >= 1 && buttons.length <= 8) return node;
            }
            return null;
        }""") if title_handle else None
        modal = modal_handle.as_element() if modal_handle else None
        if modal is None:
            continue
        scoped_titles.append(title)
        if not any(modal.evaluate("(node, other) => node === other", item) for item in modals):
            modals.append(modal)

    if not modals:
        return False
    record("promotion_detected", kind="hh_pro")

    semantic_close = []
    visual_close = []
    for modal in modals:
        modal_box = modal.bounding_box()
        if not modal_box:
            continue
        for candidate in modal.query_selector_all('button, [role="button"]'):
            if not candidate.is_visible() or not candidate.is_enabled():
                continue
            box = candidate.bounding_box()
            if box is None:
                continue
            label = (candidate.get_attribute("aria-label") or "").strip().lower()
            data_qa = (candidate.get_attribute("data-qa") or "").strip().lower()
            title = (candidate.get_attribute("title") or "").strip().lower()
            is_close_label = (
                label in {"закрыть", "close"}
                or title in {"закрыть", "close"}
                or data_qa in {"modal-close", "popup-close", "dialog-close"}
            )
            is_top_right_icon = (
                box is not None
                and not candidate.inner_text().strip()
                and box["x"] >= modal_box["x"] + modal_box["width"] * 0.7
                and box["y"] <= modal_box["y"] + modal_box["height"] * 0.25
                and box["width"] <= 80
                and box["height"] <= 80
            )
            if is_close_label:
                semantic_close.append(candidate)
            elif is_top_right_icon:
                visual_close.append(candidate)

    close_candidates = semantic_close if semantic_close else visual_close
    if len(modals) != 1 or len(close_candidates) != 1:
        record("unknown_dialog", kind="hh_pro", modals=len(modals),
               semantic_close_controls=len(semantic_close), visual_close_controls=len(visual_close))
        raise RuntimeError("HH PRO dialog has no unambiguous safe close control")

    close_candidates[0].click(timeout=2000)
    try:
        for title in scoped_titles:
            title.wait_for(state="hidden", timeout=3000)
    except PlaywrightTimeoutError as exc:
        record("unknown_dialog", kind="hh_pro", reason="close_not_confirmed")
        raise RuntimeError("HH PRO dialog remained visible after close") from exc
    record("promotion_closed", kind="hh_pro")
    return True


def dismiss_promotion(page):
    """Close known optional promotions and return whether one was dismissed."""
    dismissed = False
    # Decline only the optional cross-posting offer, scoped to its visible dialog.
    offer = page.get_by_text("Разместим его в Сетке?", exact=False)
    visible_offers = []
    for index in range(offer.count()):
        candidate = offer.nth(index)
        if not candidate.is_visible():
            continue
        handle = candidate.element_handle()
        if handle is not None and handle.evaluate("offer => {" + MODAL_TEST_JS + """
            for (let node = offer.parentElement; node && node !== document.body;
                 node = node.parentElement) {
                const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                const buttons = node.querySelectorAll('button, [role="button"]');
                if (isModal(node) && text.includes('Разместим его в Сетке?') &&
                    text.length <= 1200 && buttons.length >= 2 && buttons.length <= 4)
                    return true;
            }
            return false;
        }"""):
            visible_offers.append(candidate)
    title = page.get_by_text("Сетка", exact=True)
    visible_titles = []
    for index in range(title.count()):
        candidate = title.nth(index)
        if not candidate.is_visible():
            continue
        handle = candidate.element_handle()
        if handle is not None and handle.evaluate("title => {" + MODAL_TEST_JS + """
            for (let node = title.parentElement; node && node !== document.body;
                 node = node.parentElement) {
                const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                const buttons = node.querySelectorAll('button, [role="button"]');
                if (isModal(node) && text.length <= 1200 && buttons.length >= 2 && buttons.length <= 4)
                    return true;
            }
            return false;
        }"""):
            visible_titles.append(candidate)
    visible_markers = visible_offers + visible_titles
    if visible_markers:
        record("promotion_detected", kind="setka")
        choices = []
        for label in ("Потом", "Позже", "Позднее"):
            buttons = page.get_by_role("button", name=label, exact=True)
            for index in range(buttons.count()):
                button = buttons.nth(index)
                if not button.is_visible() or not button.is_enabled(timeout=2000):
                    continue
                handle = button.element_handle()
                if handle is not None and handle.evaluate("button => {" + MODAL_TEST_JS + """
                    for (let node = button.parentElement; node && node !== document.body;
                         node = node.parentElement) {
                        const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                        const exactTitle = Array.from(node.querySelectorAll('*')).some(el =>
                            (el.innerText || '').replace(/\\s+/g, ' ').trim() === 'Сетка' &&
                            el.getClientRects().length > 0 &&
                            window.getComputedStyle(el).visibility !== 'hidden');
                        const buttons = node.querySelectorAll('button, [role="button"]');
                        if ((text.includes('Разместим его в Сетке?') || exactTitle) && isModal(node) &&
                            text.length <= 1200 && buttons.length >= 2 && buttons.length <= 4)
                            return true;
                    }
                    return false;
                }"""):
                    choices.append(handle)
        if len(choices) != 1:
            record("unknown_dialog", kind="setka", later_controls=len(choices))
            raise RuntimeError("Setka offer has no unambiguous later choice")
        choices[0].click(timeout=2000)
        for visible in visible_markers:
            visible.wait_for(state="hidden", timeout=3000)
        record("promotion_closed", kind="setka")
        dismissed = True

    # Never click the purchase CTA; close only through the scoped safe control.
    if dismiss_hh_pro_promotion(page):
        return True
    return dismissed


def handle_dialogs(page, *, dismiss=True):
    """Dismiss only known promotions, then fail on any visible unknown modal."""
    if not is_hh_url(page.url):
        raise GlobalCycleError("Unexpected host while checking dialogs")
    if dismiss:
        for _ in range(3):
            if not dismiss_promotion(page):
                break
    visible = page.evaluate("() => {" + MODAL_TEST_JS + """
        return Array.from(document.querySelectorAll('*'))
        .filter(el => {
            if (el.getClientRects().length === 0) return false;
            const buttons = el.querySelectorAll('button, [role="button"]');
            if (buttons.length < 1 || buttons.length > 8) return false;
            const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            if (text.length > 1200) return false;
            return isModal(el);
        }).length;
    }""")
    if visible:
        record("unknown_dialog", count=visible)
        failure_artifact(page, "unknown_dialog")
        raise GlobalCycleError("Unknown dialog is blocking the resume list")


def current_bump_control(page, resume_key):
    """Use the same atomic card classifier as check and final verification."""
    if not is_profile_url(page.url):
        raise GlobalCycleError("Unexpected navigation while checking bump control")
    snapshot = card_state(page, resume_key)
    return snapshot["state"], offered_locator(page, resume_key) if snapshot["state"] == "offered" else None


def reload_resume_control(page, resume_key):
    """Reconcile a possibly submitted action without clicking it again."""
    if not is_profile_url(page.url):
        raise GlobalCycleError("Unexpected navigation before resume reconciliation")
    page.reload(wait_until="domcontentloaded", timeout=60000)
    if not is_profile_url(page.url):
        raise GlobalCycleError("Unexpected navigation after resume reconciliation")
    return current_bump_control(page, resume_key)


def click_bump(page, resume_key):
    """Send once with a live, resume-scoped locator. Errors are ambiguous."""
    button = offered_locator(page, resume_key)
    if button.count() != 1:
        raise AmbiguousResumeError("Offered action changed before click")
    button.click(timeout=2500)


def wait_for_bump_confirmation(page, resume_key):
    """Require a stable exact cooldown, then reconcile once after timeout."""
    deadline = time.monotonic() + 20
    consecutive = 0
    while time.monotonic() < deadline:
        handle_dialogs(page)
        state, _ = current_bump_control(page, resume_key)
        consecutive = consecutive + 1 if state == "cooldown" else 0
        if consecutive >= 2:
            return
        page.wait_for_timeout(300)
    record("bump_confirmation_recheck", resume=resume_key, state="reload")
    try:
        state, _ = reload_resume_control(page, resume_key)
    except (PlaywrightError, RuntimeError) as exc:
        raise AmbiguousResumeError("Bump action could not be reconciled") from exc
    record("bump_confirmation_recheck", resume=resume_key, state=state)
    if state != "cooldown":
        raise AmbiguousResumeError("Bump action was not confirmed")


def record(event, **fields):
    STATE.mkdir(exist_ok=True)
    entry = {"time": datetime.now(timezone.utc).isoformat(), "event": event,
             "run_id": RUN_ID, "code_version": CODE_VERSION, "stage": STAGE, **fields}
    with (STATE / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps(entry, ensure_ascii=False), flush=True)


def failure_artifact(page, category, *, resume=None):
    """Save a unique local diagnostic without DOM text, cookies, or storage."""
    folder = STATE / "failures" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                   + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    metadata = {"run_id": RUN_ID, "code_version": CODE_VERSION, "stage": STAGE,
                "category": category, "resume": resume, "url_host": None,
                "url_path": None, "dialogs": []}
    try:
        parsed = urlsplit(page.url)
        metadata["url_host"] = parsed.hostname
        metadata["url_path"] = parsed.path
        if is_hh_url(page.url) and not parsed.path.startswith("/account/"):
            metadata["dialogs"] = page.evaluate("() => {" + MODAL_TEST_JS + """
                return Array.from(document.querySelectorAll('*'))
                    .filter(node => node.getClientRects().length > 0 && isModal(node))
                    .slice(0, 5).map(node => {
                        const rect = node.getBoundingClientRect();
                        const controls = Array.from(node.querySelectorAll('button, [role="button"]'));
                        return {
                            tag: node.tagName,
                            role: (node.getAttribute('role') || '').slice(0, 40),
                            id: (node.id || '').slice(0, 80),
                            class_name: (typeof node.className === 'string' ? node.className : '').slice(0, 80),
                            data_qa: (node.getAttribute('data-qa') || '').slice(0, 80),
                            aria_modal: node.getAttribute('aria-modal') === 'true',
                            position: window.getComputedStyle(node).position,
                            rect: {x: Math.round(rect.x), y: Math.round(rect.y),
                                width: Math.round(rect.width), height: Math.round(rect.height)},
                            buttons: controls.length,
                            close_markers: controls.filter(control => {
                                const label = (control.getAttribute('aria-label') || '').trim().toLowerCase();
                                const title = (control.getAttribute('title') || '').trim().toLowerCase();
                                const qa = (control.getAttribute('data-qa') || '').trim().toLowerCase();
                                return ['закрыть', 'close'].includes(label) ||
                                    ['закрыть', 'close'].includes(title) ||
                                    ['modal-close', 'popup-close', 'dialog-close'].includes(qa);
                            }).length
                        };
                    });
            }""")
            page.screenshot(path=str(folder / "screen.png"),
                            full_page=category == "service_preflight", timeout=8000)
    except PlaywrightError:
        metadata["capture_error"] = "browser_unavailable"
    atomic_json(folder / "metadata.json", metadata)
    record("failure_artifact", category=category, path=str(folder.relative_to(STATE)))


def safe_failure_artifact(page, category, *, resume=None):
    try:
        failure_artifact(page, category, resume=resume)
    except (OSError, PlaywrightError):
        record("failure_artifact_unavailable", category=category, resume=resume)


def error_category(exc, page=None):
    message = str(exc).lower()
    if page is not None and page.is_closed():
        return "browser_closed"
    if any(item in message for item in ("net::", "connection", "network")):
        return "network"
    if "navigation" in message or "unexpected page" in message:
        return "navigation"
    if "auth" in message or "login" in message or "account" in message:
        return "authentication"
    if "profile" in message and "browser" in message:
        return "profile"
    return "dom"


def evaluate_dom(page, selector, script, operation):
    """Retry one read-only DOM evaluation once after the observed navigation race."""
    try:
        return page.locator(selector).evaluate_all(script)
    except PlaywrightError as exc:
        if "Execution context was destroyed, most likely because of a navigation" not in str(exc):
            raise
        record("dom_navigation_retry", operation=operation)
        page.wait_for_timeout(250)
        return page.locator(selector).evaluate_all(script)


def inventory(page):
    # Read only DOM attributes, never cookies, passwords or browser storage.
    script = """els => els.map(e => ({
        href: e.getAttribute('href'), text: e.innerText,
        qa: e.getAttribute('data-qa')
    }))"""
    return evaluate_dom(page, 'a[href*="/resume/"]', script, "inventory")


def login_page(page):
    """Recognize the observed HH phone/email sign-in screen."""
    if not is_hh_url(page.url):
        raise GlobalCycleError("Unexpected host before authentication check")
    if re.match(r"^/account/(?:login|auth)(?:/|$)", urlsplit(page.url).path):
        return True
    phone_email_login = (
        page.get_by_role("heading", name="Поиск работы", exact=True).count() > 0
        and page.get_by_role("button", name="Дальше", exact=True).count() > 0
        and page.get_by_text("Телефон", exact=True).count() > 0
        and page.get_by_text("Почта", exact=True).count() > 0
    )
    # On HH's account chooser the labels are sometimes rendered as links or
    # headings rather than buttons. body.inner_text() includes visible text
    # only, so hidden templates and stale modal markup cannot trigger this.
    visible_lines = {
        re.sub(r"\s+", " ", line).strip().casefold()
        for line in page.locator("body").inner_text().splitlines()
    }
    has_login = any(re.search(r"(?<!\w)войти(?!\w)", line) for line in visible_lines)
    has_register = any("зарегистрироваться" in line for line in visible_lines)
    has_applicant_choice = (
        "профиль соискателя" in visible_lines and has_login and has_register
    ) or (
        "войдите или зарегистрируйтесь" in visible_lines and has_login and has_register
    )
    return phone_email_login or has_applicant_choice


def wait_for_manual_login(page):
    record("auth_required", timeout_seconds=1800)
    set_stage("authentication", needs_attention=True)
    print("HH session expired. Log in manually in the open Chrome window; waiting up to 30 minutes.", flush=True)
    deadline = time.monotonic() + 1800
    last_heartbeat = 0.0
    try:
        while time.monotonic() < deadline:
            if time.monotonic() - last_heartbeat >= 30:
                set_stage("authentication", needs_attention=True,
                          remaining_seconds=max(0, int(deadline - time.monotonic())))
                last_heartbeat = time.monotonic()
            if page.is_closed():
                record("auth_timeout", reason="browser_closed")
                return False
            if not is_hh_url(page.url):
                record("auth_timeout", reason="unexpected_host")
                return False
            dismiss_cookie_notice(page)
            if is_profile_url(page.url):
                current_ids = resume_keys(page)
                if not current_ids:
                    page.wait_for_timeout(2000)
                    continue
                if CURRENT_STORE is None or not CURRENT_STORE.account_matches(current_ids):
                    record("auth_account_mismatch")
                    return False
                wait_for_cookie_notice(page)
                record("auth_restored", resumes=len(current_ids))
                return True
            page.wait_for_timeout(2000)
    except PlaywrightError as exc:
        if page.is_closed():
            record("auth_timeout", reason="browser_closed")
            return False
        record("auth_timeout", reason="dom_error", error_type=type(exc).__name__)
        raise GlobalCycleError("Authentication page could not be read") from exc
    record("auth_timeout")
    return False


def inspect(page, expected_keys=None, *, certify=False, require_cooldown=False):
    if not is_profile_url(page.url):
        raise RuntimeError("Unexpected navigation during inspection")
    keys = resume_keys(page)
    if not keys:
        raise RuntimeError("No resume cards found during inspection")
    if expected_keys is not None and set(keys) != set(expected_keys):
        raise RuntimeError("Resume list changed during inspection")
    if CURRENT_STORE is not None:
        if CURRENT_STORE.state["account_ids"]:
            if not CURRENT_STORE.account_matches(keys):
                raise GlobalCycleError("Account does not match inspection baseline")
        else:
            CURRENT_STORE.set_baseline(keys)
    controls = []
    for key in keys:
        snapshot = card_state(page, key)
        controls.append({"qa": "resume-card-link-" + key,
                         "state": snapshot["state"], "reason": snapshot.get("reason")})
    if certify:
        if any(row["state"] == "unknown" or
               (require_cooldown and row["state"] == "offered") for row in controls):
            raise GlobalCycleError("Inspection contains uncertifiable resume state")
        certification_snapshot(page, keys, require_cooldown=require_cooldown)
    atomic_json(STATE / "inspection.json", {"url": page.url,
                "resumes": [{"qa": "resume-card-link-" + key} for key in keys],
                "controls": controls})
    record("inspection_saved", resume_links=len(keys))
    return controls


def bump(page, *, dry_run=False):
    """Process cards independently; persist intent before each possible send."""
    (STATE / "live-verified.json").unlink(missing_ok=True)
    if not is_profile_url(page.url):
        raise GlobalCycleError("Unexpected page or login required")
    page.wait_for_selector('[data-qa^="resume-card-link-"]', timeout=20000)
    keys = resume_keys(page)
    if not keys:
        raise GlobalCycleError("No resume cards found")
    if CURRENT_STORE is None or not CURRENT_STORE.account_matches(keys):
        raise GlobalCycleError("Account does not match inspection baseline")
    handle_dialogs(page, dismiss=not dry_run)
    eligible = 0
    changed = 0
    unavailable = 0
    unresolved = []
    for key in keys:
        set_stage("resume", resume=key, dry_run=dry_run)
        if not is_profile_url(page.url):
            raise GlobalCycleError("Unexpected navigation during bump cycle")
        handle_dialogs(page, dismiss=not dry_run)
        try:
            snapshot = card_state(page, key)
        except (PlaywrightError, RuntimeError) as exc:
            safe_failure_artifact(page, "card_read", resume=key)
            record("resume_unresolved", resume=key, reason="read_error",
                   error_type=type(exc).__name__)
            unresolved.append(key)
            continue
        state = snapshot["state"]
        if state == "cooldown":
            if not dry_run and CURRENT_STORE.progress(key) and CURRENT_STORE.progress(key)["status"] != "confirmed":
                CURRENT_STORE.mark_confirmed(key)
            continue
        if state == "unavailable":
            unavailable += 1
            record("resume_unavailable", resume=key, reason=snapshot["reason"])
            if CURRENT_STORE.progress(key) and CURRENT_STORE.progress(key)["status"] != "confirmed":
                unresolved.append(key)
            continue
        if state == "unknown":
            record("resume_unresolved", resume=key, reason=snapshot["reason"])
            unresolved.append(key)
            continue
        eligible += 1
        if dry_run:
            continue
        if not CURRENT_STORE.may_send(key):
            record("resume_quarantined", resume=key,
                   until=CURRENT_STORE.progress(key)["not_before"])
            unresolved.append(key)
            continue
        CURRENT_STORE.mark_attempt(key)
        record("bump_attempt", resume=key)
        try:
            click_bump(page, key)
            wait_for_bump_confirmation(page, key)
        except GlobalCycleError:
            CURRENT_STORE.mark_ambiguous(key)
            raise
        except (PlaywrightError, RuntimeError) as exc:
            CURRENT_STORE.mark_ambiguous(key)
            safe_failure_artifact(page, "ambiguous_resume", resume=key)
            record("resume_ambiguous", resume=key, error_type=type(exc).__name__)
            unresolved.append(key)
            if not is_profile_url(page.url):
                raise GlobalCycleError("Navigation after ambiguous resume action") from exc
            handle_dialogs(page)
            continue
        CURRENT_STORE.mark_confirmed(key)
        changed += 1
        record("bump_confirmed", resume=key)
        page.wait_for_timeout(700)
    if dry_run:
        record("dry_run", eligible=eligible, total=len(keys), unavailable=unavailable,
               unresolved=len(unresolved))
        if unresolved:
            return 1
        write_live_marker("check", page=page, expected_keys=keys,
                          total=len(keys), eligible=eligible, unavailable=unavailable)
        return 0

    # Recheck every card through the same classifier after a server refresh.
    page.reload(wait_until="domcontentloaded", timeout=60000)
    if not is_profile_url(page.url):
        raise GlobalCycleError("Unexpected navigation during final verification")
    handle_dialogs(page)
    after = resume_keys(page)
    if set(after) != set(keys):
        raise GlobalCycleError("Resume list changed during final verification")
    states = {key: card_state(page, key)["state"] for key in keys}
    for key, state in states.items():
        if state == "cooldown" and CURRENT_STORE.progress(key) and CURRENT_STORE.progress(key)["status"] != "confirmed":
            CURRENT_STORE.mark_confirmed(key)
    remaining = sum(state == "offered" for state in states.values())
    unknown = sum(state == "unknown" for state in states.values())
    unavailable = sum(state == "unavailable" for state in states.values())
    unavailable_pending = sum(
        state == "unavailable" and CURRENT_STORE.progress(key) is not None and
        CURRENT_STORE.progress(key)["status"] != "confirmed"
        for key, state in states.items())
    if remaining or unknown or unavailable_pending:
        record("verification_partial", raised=changed, remaining=remaining,
               unknown=unknown, unavailable=unavailable,
               unavailable_pending=unavailable_pending, unresolved=len(unresolved))
        return 1
    if code_version() != CODE_VERSION:
        raise GlobalCycleError("Code changed during bump cycle; refusing certification")
    inspect(page, expected_keys=keys, certify=True, require_cooldown=True)
    now = time.time()
    CURRENT_STORE.state["last_run"] = now
    CURRENT_STORE.state["next_due"] = now + INTERVAL
    CURRENT_STORE.save()
    atomic_json(STATE / "last-run.json", {"time": now, "code_version": CODE_VERSION})
    write_live_marker("run", page=page, expected_keys=keys,
                      total=len(keys), raised=changed, unavailable=unavailable,
                      script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    record("run_verified", raised=changed, total=len(keys), remaining=0,
           unavailable=unavailable)
    return 0


def service_health(page):
    """Read account and card states in the existing tab without bumping."""
    (STATE / "live-verified.json").unlink(missing_ok=True)
    if not is_hh_url(page.url):
        raise GlobalCycleError("Unexpected host during service preflight")
    if login_page(page):
        raise GlobalCycleError("Authentication required in the open browser")
    if not is_profile_url(page.url):
        raise GlobalCycleError("Unexpected page during service preflight")
    page.wait_for_selector('[data-qa^="resume-card-link-"]', state="visible", timeout=20000)
    handle_dialogs(page)
    keys = resume_keys(page)
    if not keys or not CURRENT_STORE.account_matches(keys):
        raise GlobalCycleError("Account does not match inspection baseline")
    snapshots = {key: card_state(page, key) for key in keys}
    for key, snapshot in snapshots.items():
        if snapshot["state"] == "unknown":
            record("preflight_card_unknown", resume=key,
                   reason=snapshot.get("reason"), link_count=snapshot.get("link_count"),
                   visible_link_count=snapshot.get("visible_link_count"),
                   card_tag=snapshot.get("card_tag"),
                   card_text_length=snapshot.get("card_text_length"),
                   action_count=snapshot.get("action_count"),
                   cooldown_count=snapshot.get("cooldown_count"),
                   qa_count=snapshot.get("qa_count"),
                   visibility_count=snapshot.get("visibility_count"),
                   candidate_controls=snapshot.get("candidate_controls", []),
                   qa_controls=snapshot.get("qa_controls", []),
                   ancestors=snapshot.get("ancestors", []),
                   descendant_qa=snapshot.get("descendant_qa", []))
    states = [snapshot["state"] for snapshot in snapshots.values()]
    if "unknown" in states:
        raise GlobalCycleError("Unknown resume card during service preflight")
    write_live_marker("check", page=page, expected_keys=keys,
                      total=len(keys), eligible=states.count("offered"),
                      unavailable=states.count("unavailable"))
    record("service_health", resumes=len(keys), offered=states.count("offered"),
           cooldown=states.count("cooldown"), unavailable=states.count("unavailable"),
           unknown=states.count("unknown"))


def serve(page):
    """Keep one persistent tab, with durable due time and bounded recovery."""
    store = CURRENT_STORE
    record("service_started")
    display_attempted = False
    last_heartbeat = 0.0
    health_ok = False
    next_health_at = 0.0
    preflight_failures = 0
    preflight_artifact_saved = False
    attention_reason = None
    announced_due = None
    while not page.is_closed():
        now = time.time()
        due = store.due_at(now)
        if not health_ok:
            if now >= next_health_at:
                try:
                    set_stage("preflight")
                    if not display_attempted:
                        display_attempted = True
                        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                    dismiss_cookie_notice(page)
                    service_health(page)
                except Exception as exc:
                    if page.is_closed():
                        break
                    preflight_failures += 1
                    attention_reason = error_category(exc, page)
                    if not preflight_artifact_saved:
                        safe_failure_artifact(page, "service_preflight")
                        preflight_artifact_saved = True
                    record("service_preflight_failed", category=attention_reason,
                           error_type=type(exc).__name__, failures=preflight_failures)
                    next_health_at = time.time() + 60
                    set_stage("needs_attention", reason=attention_reason,
                              preflight_failures=preflight_failures,
                              error_type=type(exc).__name__)
                else:
                    health_ok = True
                    preflight_failures = 0
                    preflight_artifact_saved = False
                    attention_reason = None
                    set_stage("waiting", next_due=store.due_at())
            if not health_ok:
                if time.monotonic() - last_heartbeat >= 30:
                    set_stage("needs_attention", reason=attention_reason,
                              preflight_failures=preflight_failures)
                    last_heartbeat = time.monotonic()
                try:
                    page.wait_for_timeout(1000)
                except PlaywrightError:
                    if page.is_closed():
                        break
                    raise
                continue
        remaining = due - now
        if remaining > 0:
            if announced_due != due:
                record("service_waiting", next_due=due,
                       next_due_local=datetime.fromtimestamp(due).astimezone().isoformat())
                announced_due = due
            if time.monotonic() - last_heartbeat >= 30:
                set_stage("waiting", next_due=due)
                last_heartbeat = time.monotonic()
            try:
                page.wait_for_timeout(min(1000, max(1, int(remaining * 1000))))
            except PlaywrightError:
                if page.is_closed():
                    break
                raise
            continue

        # A crash at any point after this write cannot cause an immediate resend.
        store.state["last_cycle"] = now
        store.state["next_due"] = now + INTERVAL
        store.save()
        success = False
        category = "partial"
        try:
            set_stage("navigation")
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            display_attempted = True
            if not is_hh_url(page.url):
                raise GlobalCycleError("Unexpected host after navigation")
            wait_for_cookie_notice(page)
            if login_page(page):
                set_stage("authentication")
                if not wait_for_manual_login(page):
                    raise GlobalCycleError("Authentication unavailable or account mismatch")
            set_stage("bump")
            result = bump(page)
            success = result == 0
            if not success:
                safe_failure_artifact(page, "partial_cycle")
                record("service_cycle_failed", category="partial", error_type="VerificationError")
        except Exception as exc:
            if page.is_closed():
                break
            category = error_category(exc, page)
            try:
                failure_artifact(page, category)
            except (OSError, PlaywrightError):
                pass
            record("service_cycle_failed", category=category, error_type=type(exc).__name__)
        if page.is_closed():
            break
        if success:
            store.cycle_finished(success=True)
            set_stage("waiting", next_due=store.due_at())
        else:
            failures = store.state["failure_count"] + 1
            delay = min(300, 60 * (2 ** min(failures - 1, 3)))
            store.cycle_finished(success=False, delay=delay)
            set_stage("retry_wait", next_due=store.due_at(), category=category)
            health_ok = False
            next_health_at = time.time() + 60
            if store.state["failure_count"] >= 3:
                record("service_stopped", reason="failure_limit",
                       failure_count=store.state["failure_count"])
                return 1
    record("service_stopped", reason="browser_closed")
    return 0
def main():
    global CURRENT_STORE
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=["login", "inspect", "check", "run", "service"])
    parser.add_argument("--version", action="store_true", help="Print production code digest")
    args = parser.parse_args()
    if args.version:
        print(json.dumps({"code_version": CODE_VERSION}))
        return 0
    if args.mode is None:
        parser.error("mode is required")
    STATE.mkdir(exist_ok=True)
    # Windows releases this byte lock automatically after a crash.
    import msvcrt
    with (STATE / "lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) and \
               getattr(exc, "winerror", None) not in (32, 33, 36):
                raise
            record("already_running")
            return 3
        if os.fstat(lock.fileno()).st_size == 0:
            lock.seek(0)
            lock.write(b"0")
            lock.flush()
        CURRENT_STORE = Store(STATE, INTERVAL)
        if CURRENT_STORE.invalid_files:
            record("state_quarantined", files=CURRENT_STORE.invalid_files)
        set_stage("startup")
        if args.mode == "run" and time.time() < CURRENT_STORE.due_at():
            record("not_due", next_due=CURRENT_STORE.due_at())
            return 0
        keep_open_marker = STATE / "keep-open.once"
        keep_browser_open = keep_open_marker.exists()
        if keep_browser_open:
            keep_open_marker.unlink()
        with sync_playwright() as p:
            context = None
            page = None
            try:
                context = p.chromium.launch_persistent_context(
                    str(STATE / "profile"), channel="chrome", headless=False,
                    chromium_sandbox=True,
                )
                record("browser_profile", path=str((STATE / "profile").resolve()),
                       windows_user=getpass.getuser())
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(15000)
                if args.mode == "service":
                    return serve(page)
                set_stage("navigation")
                page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                if not is_hh_url(page.url):
                    raise GlobalCycleError("Unexpected host after navigation")
                wait_for_cookie_notice(page)
                if args.mode in ("check", "run"):
                    if args.mode == "run" and login_page(page):
                        if not wait_for_manual_login(page):
                            return 2
                    set_stage("check" if args.mode == "check" else "bump")
                    result = bump(page, dry_run=args.mode == "check")
                    if args.mode == "run":
                        CURRENT_STORE.cycle_finished(success=result == 0,
                            delay=60 if result else None)
                    set_stage("complete" if result == 0 else "partial", exit_code=result)
                    return result
                if args.mode == "login":
                    record("waiting_for_login", timeout_seconds=900)
                    deadline = time.monotonic() + 900
                    while time.monotonic() < deadline:
                        if page.is_closed():
                            record("login_window_closed")
                            return 2
                        dismiss_cookie_notice(page)
                        if is_profile_url(page.url) and inventory(page):
                            wait_for_cookie_notice(page)
                            inspect(page)
                            record("login_ready")
                            return 0
                        page.wait_for_timeout(2000)
                    record("login_timeout")
                    return 2
                page.wait_for_timeout(2500)
                inspect(page)
                return 0
            except Exception as exc:
                set_stage("failed", category=error_category(exc, page))
                if page is not None and not page.is_closed():
                    try:
                        failure_artifact(page, error_category(exc, page))
                    except (OSError, PlaywrightError):
                        pass
                print(str(exc)[:2500].encode("ascii", errors="backslashreplace").decode("ascii"), flush=True)
                raise
            finally:
                if context is not None:
                    if keep_browser_open and page is not None and not page.is_closed():
                        record("browser_kept_open", until="manual_close_or_task_limit")
                        try:
                            while not page.is_closed():
                                page.wait_for_timeout(1000)
                        except PlaywrightError:
                            pass
                    context.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Exceptions may include page snippets; don't copy them to persistent logs.
        record("failed", error_type=type(exc).__name__)
        sys.exit(1)
