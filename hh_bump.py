"""Local HH resume bumping through the visible website, without HH API keys."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
URL = "https://hh.ru/applicant/profile/me"
INTERVAL = 4 * 60 * 60 + 60


def dismiss_promotion(page):
    """Close known optional promotions and return whether one was dismissed."""
    dismissed = False
    # Decline only the observed optional cross-posting offer; never accept it.
    offer = page.get_by_text("Разместим его в Сетке?", exact=False)
    later = page.get_by_role("button", name="Потом", exact=True)
    if offer.count() == 1 and offer.is_visible() and later.count() == 1 and later.is_visible():
        later.click()
        offer.wait_for(state="hidden")
        dismissed = True

    # HH can instead show an hh PRO purchase dialog after a bump. It has no
    # stable data-qa, so scope the close target to the observed dialog title and
    # only click its small, empty control in the top-right corner. Plan buttons
    # and the purchase CTA are deliberately outside that area.
    pro_title = page.get_by_text("Хотите быстрее получить приглашение?", exact=True)
    if pro_title.count() == 1 and pro_title.is_visible():
        title_handle = pro_title.element_handle()
        modal_handle = title_handle.evaluate_handle("""title => {
            for (let node = title; node && node !== document.body; node = node.parentElement) {
                if ((node.innerText || '').includes('Подключить hh PRO') &&
                    node.querySelector('button, [role="button"]')) return node;
            }
            return null;
        }""") if title_handle else None
        modal = modal_handle.as_element() if modal_handle else None
        if modal:
            modal_box = modal.bounding_box()
            if modal_box:
                for candidate in modal.query_selector_all('button, [role="button"]'):
                    box = candidate.bounding_box()
                    label = (candidate.get_attribute("aria-label") or "").strip().lower()
                    is_close_label = label in {"закрыть", "close"}
                    is_top_right_icon = (
                        box is not None
                        and not candidate.inner_text().strip()
                        and box["x"] >= modal_box["x"] + modal_box["width"] * 0.7
                        and box["y"] <= modal_box["y"] + modal_box["height"] * 0.25
                        and box["width"] <= 80
                        and box["height"] <= 80
                    )
                    if is_close_label or is_top_right_icon:
                        candidate.click()
                        # The dialog is animated by HH. Do not turn a delayed
                        # animation into a 30-second Playwright timeout; the
                        # caller rechecks it before continuing the bump.
                        page.wait_for_timeout(100)
                        return not pro_title.is_visible()
    return dismissed


def wait_for_bump_confirmation(page, button, original_button):
    """Keep the page usable while waiting for HH to replace the bump control."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        dismiss_promotion(page)
        try:
            if not original_button.is_visible():
                return
        except PlaywrightError:
            # HH may replace the element instead of hiding it. Confirm with the
            # original scoped locator, after optional modal windows are closed.
            if button.count() == 0 or not button.is_visible():
                return
        page.wait_for_timeout(300)
    raise RuntimeError("Bump action was not confirmed")


def record(event, **fields):
    STATE.mkdir(exist_ok=True)
    entry = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    with (STATE / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps(entry, ensure_ascii=False), flush=True)


def inventory(page):
    # Read only DOM attributes, never cookies, passwords or browser storage.
    return page.locator('a[href*="/resume/"]').evaluate_all("""els => els.map(e => ({
        href: e.getAttribute('href'), text: e.innerText,
        qa: e.getAttribute('data-qa')
    }))""")


def login_page(page):
    """Recognize the observed HH phone/email sign-in screen."""
    if re.search(r"/account/(?:login|auth)(?:/|$)", page.url):
        return True
    return (
        page.get_by_role("heading", name="Поиск работы", exact=True).count() > 0
        and page.get_by_role("button", name="Дальше", exact=True).count() > 0
        and page.get_by_text("Телефон", exact=True).count() > 0
        and page.get_by_text("Почта", exact=True).count() > 0
    )


def wait_for_manual_login(page):
    record("auth_required", timeout_seconds=1800)
    print("HH session expired. Log in manually in the open Chrome window; waiting up to 30 minutes.", flush=True)
    deadline = time.monotonic() + 1800
    try:
        while time.monotonic() < deadline:
            if page.is_closed():
                record("auth_timeout", reason="browser_closed")
                return False
            cards = page.locator('[data-qa^="resume-card-link-"]')
            if page.url.startswith(URL) and cards.count() and inventory(page):
                prior_file = STATE / "inspection.json"
                prior = json.loads(prior_file.read_text(encoding="utf-8")) if prior_file.exists() else {}
                def resume_ids(items):
                    return {item.get("qa") or item.get("href") for item in items
                            if item.get("qa") or item.get("href")}
                prior_ids = resume_ids(prior.get("resumes", []))
                current_ids = resume_ids(inventory(page))
                if not prior_ids.intersection(current_ids):
                    record("auth_account_mismatch")
                    return False
                record("auth_restored", resumes=len(current_ids))
                return True
            page.wait_for_timeout(2000)
    except PlaywrightError:
        record("auth_timeout", reason="browser_closed")
        return False
    record("auth_timeout")
    return False


def inspect(page):
    page.screenshot(path=str(STATE / "inspection.png"), full_page=True)
    controls = page.locator('button, a, [role="button"]').evaluate_all("""els => els
        .filter(e => e.getClientRects().length && /подня|обнов|резюме/i.test(e.innerText))
        .map(e => ({tag:e.tagName, text:e.innerText, qa:e.getAttribute('data-qa'),
            disabled:e.disabled || e.getAttribute('aria-disabled') === 'true'}))""")
    (STATE / "inspection.json").write_text(json.dumps({"url": page.url,
        "resumes": inventory(page), "controls": controls}, ensure_ascii=False, indent=2), encoding="utf-8")
    record("inspection_saved", resume_links=len(inventory(page)))


def bump(page, *, dry_run=False):
    page.wait_for_selector('[data-qa^="resume-card-link-"]', timeout=20000)
    if not page.url.startswith(URL):
        raise RuntimeError("Unexpected page or login required")
    links = inventory(page)
    keys = list(dict.fromkeys(x["qa"] for x in links
        if x.get("qa", "").startswith("resume-card-link-")))
    if not keys:
        raise RuntimeError("No resume cards found")
    changed = 0
    for key in keys:
        if not dry_run:
            dismiss_promotion(page)
        if not page.url.startswith(URL):
            raise RuntimeError("Unexpected navigation")
        link = page.locator(f'[data-qa="{key}"]')
        # Closest ancestor containing this resume and the observed bump control.
        card = link.locator('xpath=ancestor::*[.//*[@data-qa="resume-update-button"]][1]')
        if not card.count():
            continue
        if card.locator('[data-qa^="resume-card-link-"]').count() != 1:
            # A higher common ancestor means this particular card has no button.
            continue
        button = card.locator('[data-qa="resume-update-button"]')
        if button.count() != 1 or not button.is_visible() or not button.is_enabled():
            continue
        if re.sub(r"\s+", " ", button.inner_text()).strip() != "Поднять в поиске":
            raise RuntimeError("Bump control changed")
        if dry_run:
            changed += 1
            continue
        record("bump_attempt", resume=key.removeprefix("resume-card-link-"))
        original_button = button.element_handle()
        if original_button is None:
            raise RuntimeError("Bump control disappeared before click")
        original_button.click()
        # Confirm the action by disappearance of this card's action, not click alone.
        wait_for_bump_confirmation(page, button, original_button)
        changed += 1
        record("bump_confirmed", resume=key.removeprefix("resume-card-link-"))
        page.wait_for_timeout(700)
        dismiss_promotion(page)
    if dry_run:
        record("dry_run", eligible=changed, total=len(keys))
        return 0
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector('[data-qa^="resume-card-link-"]', timeout=20000)
    page.wait_for_timeout(2500)
    after_keys = {x.get("qa") for x in inventory(page) if x.get("qa", "").startswith("resume-card-link-")}
    if after_keys != set(keys) or not page.url.startswith(URL):
        raise RuntimeError("Resume list changed during verification")
    remaining = page.locator('[data-qa="resume-update-button"]:visible').count()
    if remaining:
        record("verification_failed", raised=changed, remaining=remaining)
        return 1
    inspect(page)
    now = time.time()
    (STATE / "last-run.json").write_text(json.dumps({"time": now}), encoding="utf-8")
    if changed:
        (STATE / "live-verified.json").write_text(json.dumps({"time": now, "raised": changed}), encoding="utf-8")
    record("run_verified", raised=changed, total=len(keys), remaining=remaining)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["login", "inspect", "check", "run"])
    args = parser.parse_args()
    STATE.mkdir(exist_ok=True)
    # Windows releases this byte lock automatically after a crash.
    import msvcrt
    with (STATE / "lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            record("already_running")
            return 0
        if args.mode == "run" and (STATE / "last-run.json").exists():
            last = json.loads((STATE / "last-run.json").read_text(encoding="utf-8"))["time"]
            if time.time() - last < INTERVAL:
                record("not_due")
                return 0
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(STATE / "profile"), channel="chrome", headless=False,
                chromium_sandbox=True,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(15000)
                page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                cookie_notice = page.get_by_role("button", name="Понятно", exact=True)
                if cookie_notice.count() == 1 and cookie_notice.is_visible():
                    cookie_notice.click()
                if args.mode in ("check", "run"):
                    if args.mode == "run" and login_page(page):
                        if not wait_for_manual_login(page):
                            return 2
                    return bump(page, dry_run=args.mode == "check")
                if args.mode == "login":
                    record("waiting_for_login", timeout_seconds=900)
                    deadline = time.monotonic() + 900
                    while time.monotonic() < deadline:
                        if page.is_closed():
                            record("login_window_closed")
                            return 2
                        if page.url.startswith("https://hh.ru/applicant/") and inventory(page):
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
                if not page.is_closed():
                    page.screenshot(path=str(STATE / "failure.png"))
                print(str(exc)[:2500].encode("ascii", errors="backslashreplace").decode("ascii"), flush=True)
                raise
            finally:
                context.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Exceptions may include page snippets; don't copy them to persistent logs.
        record("failed", error_type=type(exc).__name__)
        sys.exit(1)
