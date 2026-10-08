"""Local HTML and state tests. Every browser request is fulfilled or aborted here."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright

import hh_bump
from hh_state import Store
from hh_ui import card_state, offered_locator, resume_keys

A = "a" * 40
B = "b" * 40
SHORT = "c" * 38


def card(key, control):
    return (f'<section class="resume-card"><a data-qa="resume-card-link-{key}" '
            f'href="/resume/{key}">Резюме</a>{control}</section>')


OFFERED = '<button data-qa="resume-update-button">Поднять в поиске</button>'
COOLDOWN_QA = '<button data-qa="resume-update-button">Поднять в <span>12:34</span></button>'
COOLDOWN_NO_QA = '<div>Поднять в <span>12:34</span></div>'
VISIBILITY = ('<a data-qa="resume-recommendations__button_changeVisibility">'
              '<span data-qa="resume-recommendations__button_changeVisibility-text">'
              'Сделать видимым</span></a>')


class BrowserFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.route("**/*", lambda route: route.abort())

    def tearDown(self):
        self.page.close()

    def test_card_states_and_null_qa(self):
        self.page.set_content(card(SHORT, OFFERED) + card(B, COOLDOWN_QA) +
                              '<a href="/resume/other">other</a>')
        self.assertEqual(resume_keys(self.page), [SHORT, B])
        self.assertEqual(card_state(self.page, SHORT)["state"], "offered")
        self.assertEqual(card_state(self.page, B)["state"], "cooldown")
        self.page.locator(f'[data-qa="resume-card-link-{B}"]').locator(
            "xpath=..").locator('[data-qa="resume-update-button"]').evaluate(
                "el => el.removeAttribute('data-qa')")
        self.assertEqual(card_state(self.page, B)["state"], "cooldown")
        self.assertEqual(card_state(self.page, "c" * 40)["state"], "unknown")

    def test_resume_ids_are_opaque_and_selector_safe(self):
        self.page.set_content(card(SHORT, OFFERED) + card(A, OFFERED))
        self.page.evaluate("""() => { const el = document.createElement('a');
            el.setAttribute('data-qa', 'resume-card-link-\\\" ] button');
            document.body.appendChild(el); }""")
        self.assertEqual(resume_keys(self.page), [SHORT, A])
        for malicious in ('" ] button', "x" * 129, "a.b", ""):
            with self.assertRaises(ValueError):
                card_state(self.page, malicious)
            with self.assertRaises(ValueError):
                offered_locator(self.page, malicious)

    def test_live_locator_after_rerender(self):
        self.page.set_content(card(A, OFFERED) + card(B, OFFERED))
        target = offered_locator(self.page, A)
        self.page.locator(f'[data-qa="resume-card-link-{A}"]').locator(
            "xpath=..").locator("button").evaluate(
                "el => el.outerHTML = '<button data-qa=\"resume-update-button\">Поднять в поиске</button>'")
        target.click()
        self.assertEqual(card_state(self.page, B)["state"], "offered")
        self.assertEqual(target.count(), 1)

    def test_wrong_button_without_qa_is_unknown(self):
        self.page.set_content(card(A, '<button>Поднять в поиске</button>'))
        self.assertEqual(card_state(self.page, A)["state"], "unknown")
        self.assertEqual(offered_locator(self.page, A).count(), 0)

    def test_tag_agnostic_qa_action_and_bounded_unknown_diagnostics(self):
        for control in ('<a data-qa="resume-update-button" href="#">Поднять в поиске</a>',
                        '<span data-qa="resume-update-button">Поднять в поиске</span>'):
            self.page.set_content(card(A, control))
            self.assertEqual(card_state(self.page, A)["state"], "offered")
            self.assertEqual(offered_locator(self.page, A).count(), 1)
        self.page.set_content(card(A, '<span data-qa="resume-update-button">Другая надпись</span>'))
        snapshot = card_state(self.page, A)
        self.assertEqual(snapshot["state"], "unknown")
        self.assertEqual(snapshot["qa_controls"],
                         [{"tag": "SPAN", "role": "", "known_label": None, "disabled": False}])
        self.assertEqual(offered_locator(self.page, A).count(), 0)

    def test_visibility_unavailable_only_without_bump_or_cooldown(self):
        variants = (
            (VISIBILITY + OFFERED, "offered"),
            (VISIBILITY + COOLDOWN_NO_QA, "cooldown"),
            (VISIBILITY, "unavailable"),
            ('<a data-qa="resume-recommendations__button_changeVisibility">Позже</a>', "unknown"),
            ('<a>Сделать видимым</a>', "unknown"),
        )
        for control, expected in variants:
            self.page.set_content(card(A, control))
            snapshot = card_state(self.page, A)
            self.assertEqual(snapshot["state"], expected)
            if expected == "unavailable":
                self.assertEqual(snapshot["reason"], "visibility_control")
                self.assertEqual(offered_locator(self.page, A).count(), 0)

    def test_visibility_unavailable_is_certifiable_without_click(self):
        self.page.set_content(card(A, VISIBILITY))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as state_dir:
            state_dir = Path(state_dir)
            store = Store(state_dir, hh_bump.INTERVAL)
            store.set_baseline([A])
            with patch.object(hh_bump, "STATE", state_dir), \
                 patch.object(hh_bump, "CURRENT_STORE", store), \
                 patch.object(hh_bump, "is_hh_url", return_value=True), \
                 patch.object(hh_bump, "is_profile_url", return_value=True), \
                 patch.object(self.page, "reload"), \
                 patch.object(hh_bump, "record") as record:
                hh_bump.service_health(self.page)
                self.assertEqual(json.loads((state_dir / "live-verified.json").read_text())
                                 ["unavailable"], 1)
                self.assertEqual(hh_bump.bump(self.page), 0)
                marker = json.loads((state_dir / "live-verified.json").read_text())
                self.assertEqual(marker["validation"], "run")
                self.assertEqual(marker["unavailable"], 1)
                self.assertFalse(any(call.args == ("bump_attempt",)
                                     for call in record.call_args_list))

    def test_preflight_artifact_captures_full_page_locally(self):
        self.page.set_content('<div style="height:1800px">Offline fixture</div>')
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as state_dir, \
             patch.object(hh_bump, "STATE", Path(state_dir)), \
             patch.object(hh_bump, "is_hh_url", return_value=True), \
             patch.object(hh_bump, "record"):
            hh_bump.failure_artifact(self.page, "service_preflight")
            folder = next((Path(state_dir) / "failures").iterdir())
            image = (folder / "screen.png").read_bytes()
            self.assertGreater(int.from_bytes(image[20:24], "big"), 1000)
            metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
            self.assertNotIn("Offline fixture", json.dumps(metadata))

    def test_check_and_preflight_version_marker_fail_closed(self):
        self.page.set_content(card(A, OFFERED) + card(B, COOLDOWN_NO_QA))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as state_dir:
            state_dir = Path(state_dir)
            store = Store(state_dir, hh_bump.INTERVAL)
            store.set_baseline([A, B])
            marker = state_dir / "live-verified.json"
            with patch.object(hh_bump, "STATE", state_dir), \
                 patch.object(hh_bump, "CURRENT_STORE", store), \
                 patch.object(hh_bump, "is_hh_url", return_value=True), \
                 patch.object(hh_bump, "is_profile_url", return_value=True), \
                 patch.object(hh_bump, "login_page", return_value=False), \
                 patch.object(hh_bump, "record") as record:
                self.assertEqual(hh_bump.bump(self.page, dry_run=True), 0)
                self.assertEqual(json.loads(marker.read_text())["validation"], "check")
                hh_bump.service_health(self.page)
                self.assertEqual(json.loads(marker.read_text())["eligible"], 1)
                self.page.locator(f'[data-qa="resume-card-link-{B}"]').locator(
                    "xpath=..").locator("div").evaluate("el => el.innerText = 'неизвестно'")
                with self.assertRaises(hh_bump.GlobalCycleError):
                    hh_bump.service_health(self.page)
                unknown = [call.kwargs for call in record.call_args_list
                           if call.args == ("preflight_card_unknown",)]
                self.assertEqual(len(unknown), 1)
                self.assertEqual(unknown[0]["resume"], B)
                self.assertEqual(unknown[0]["reason"], "ambiguous_or_missing_control")
                self.assertTrue(unknown[0]["ancestors"])
                self.assertTrue(unknown[0]["descendant_qa"])
                self.assertNotIn("неизвестно", json.dumps(unknown[0], ensure_ascii=False))
                self.assertEqual(hh_bump.bump(self.page, dry_run=True), 1)
                self.assertFalse(marker.exists())
                with patch.object(hh_bump, "code_version", return_value="changed"):
                    self.page.locator(f'[data-qa="resume-card-link-{B}"]').locator(
                        "xpath=..").locator("div").evaluate("el => el.innerText = 'Поднять в 12:34'")
                    with self.assertRaises(hh_bump.GlobalCycleError):
                        hh_bump.bump(self.page, dry_run=True)
                self.assertFalse(marker.exists())
                original_keys = hh_bump.resume_keys
                calls = 0

                def change_before_certificate(page):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        page.locator(f'[data-qa="resume-card-link-{B}"]').locator(
                            "xpath=..").locator("div").evaluate("el => el.innerText = 'неизвестно'")
                    return original_keys(page)

                with patch.object(hh_bump, "resume_keys", side_effect=change_before_certificate):
                    with self.assertRaises(hh_bump.GlobalCycleError):
                        hh_bump.bump(self.page, dry_run=True)
                self.assertFalse(marker.exists())
                self.page.locator(f'[data-qa="resume-card-link-{B}"]').locator(
                    "xpath=..").locator("div").evaluate("el => el.innerText = 'Поднять в 12:34'")
                profile_reads = 0

                def navigate_before_certificate(url):
                    nonlocal profile_reads
                    profile_reads += 1
                    return profile_reads < 4

                with patch.object(hh_bump, "is_profile_url", side_effect=navigate_before_certificate):
                    with self.assertRaises(hh_bump.GlobalCycleError):
                        hh_bump.bump(self.page, dry_run=True)
                self.assertFalse(marker.exists())

    def test_setka_and_hh_pro_and_unknown(self):
        for label in ("Потом", "Позже", "Позднее"):
            self.page.set_content('<div role="dialog"><h2>Сетка</h2>'
                f'<button onclick="this.closest(\'[role=dialog]\').remove()">{label}</button>'
                '<button>Разместить</button></div>')
            with patch.object(hh_bump, "record"):
                self.assertTrue(hh_bump.dismiss_promotion(self.page))
            self.assertEqual(self.page.locator('[role="dialog"]').count(), 0)
        self.page.set_content('<div role="dialog"><h2>hh PRO:</h2>'
            '<button aria-label="Закрыть" onclick="this.closest(\'[role=dialog]\').remove()">×</button>'
            '<button>Подключить hh PRO</button></div>')
        with patch.object(hh_bump, "record"):
            self.assertTrue(hh_bump.dismiss_hh_pro_promotion(self.page))
        self.page.set_content('<div role="dialog" style="position:relative;width:400px;height:240px">'
            '<h2>hh PRO:</h2>'
            '<button aria-label="Закрыть" onclick="this.closest(\'[role=dialog]\').remove()">×</button>'
            '<button style="position:absolute;right:4px;top:4px;width:20px;height:20px"></button>'
            '<button>Подключить hh PRO</button></div>')
        with patch.object(hh_bump, "record"):
            self.assertTrue(hh_bump.dismiss_hh_pro_promotion(self.page))
        self.page.set_content('<div role="dialog" style="position:relative;width:400px;height:240px">'
            '<h2>hh PRO:</h2>'
            '<button style="position:absolute;right:4px;top:4px;width:20px;height:20px"></button>'
            '<button style="position:absolute;right:35px;top:4px;width:20px;height:20px"></button>'
            '<button>Подключить hh PRO</button></div>')
        with patch.object(hh_bump, "record"):
            with self.assertRaises(RuntimeError):
                hh_bump.dismiss_hh_pro_promotion(self.page)
        self.page.set_content('<div role="dialog" style="position:relative;width:400px;height:240px">'
            '<h2>hh PRO:</h2><button aria-label="Закрыть" style="visibility:hidden">×</button>'
            '<button style="position:absolute;right:4px;top:4px;width:20px;height:20px" '
            'onclick="this.closest(\'[role=dialog]\').remove()"></button>'
            '<button>Подключить hh PRO</button></div>')
        with patch.object(hh_bump, "record"):
            self.assertTrue(hh_bump.dismiss_hh_pro_promotion(self.page))
        self.page.set_content('<div role="dialog" style="visibility:hidden">'
                              '<h2>Неизвестная акция</h2><button>Оплатить</button></div>')
        with patch.object(hh_bump, "is_hh_url", return_value=True), \
             patch.object(hh_bump, "record"):
            hh_bump.handle_dialogs(self.page)
        self.page.set_content('<button aria-haspopup="dialog">Открыть помощь</button>')
        with patch.object(hh_bump, "is_hh_url", return_value=True), \
             patch.object(hh_bump, "record"):
            hh_bump.handle_dialogs(self.page)
        self.page.set_content('<div role="dialog"><h2>Неизвестная акция</h2>'
                              '<button>Оплатить</button></div>')
        with patch.object(hh_bump, "is_hh_url", return_value=True), \
             patch.object(hh_bump, "failure_artifact"), \
             patch.object(hh_bump, "record"):
            with self.assertRaises(hh_bump.GlobalCycleError):
                hh_bump.handle_dialogs(self.page)
            self.page.set_content('<div class="popup" style="position:fixed;width:50vw;height:40vh">'
                                  '<h2>Новая акция</h2><button>Купить</button></div>')
            with self.assertRaises(hh_bump.GlobalCycleError):
                hh_bump.handle_dialogs(self.page)

    def test_partial_cycle_persists_ambiguous_and_never_resends(self):
        server = {A: "offered", B: "offered"}
        clicks = {A: 0, B: 0}

        def page_html():
            def control(key):
                if server[key] == "cooldown":
                    return COOLDOWN_NO_QA
                return ('<button data-qa="resume-update-button" '
                        f'onclick="fetch(\'/bump/{key}\', {{method: \'POST\'}}).then(() => '
                        'this.outerHTML = \'<div>Поднять в 12:34</div>\')">'
                        'Поднять в поиске</button>')
            return '<html><head><meta charset="utf-8"></head><body>' + card(A, control(A)) + card(B, control(B)) + '</body></html>'

        def route_request(route):
            url = route.request.url
            if "/bump/" in url:
                key = url.rsplit("/", 1)[-1]
                clicks[key] += 1
                if key == A:
                    server[key] = "cooldown"
                route.fulfill(status=200, body="ok")
            elif url.endswith("/applicant/profile/me"):
                route.fulfill(status=200, content_type="text/html", body=page_html())
            else:
                route.abort()

        self.page.unroute("**/*")
        self.page.route("**/*", route_request)
        self.page.goto("https://ekaterinburg.hh.ru/applicant/profile/me")
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as state_dir:
            state_dir = Path(state_dir)
            store = Store(state_dir, hh_bump.INTERVAL)
            store.set_baseline([A, B])
            original_wait = hh_bump.wait_for_bump_confirmation

            def wait_result(page, key):
                if key == B:
                    raise hh_bump.AmbiguousResumeError("fixture uncertain click")
                return original_wait(page, key)

            with patch.object(hh_bump, "STATE", state_dir), \
                 patch.object(hh_bump, "CURRENT_STORE", store), \
                 patch.object(hh_bump, "wait_for_bump_confirmation", side_effect=wait_result):
                self.assertEqual(hh_bump.bump(self.page), 1)
                self.assertEqual(store.progress(B)["status"], "ambiguous")
                self.assertFalse(store.may_send(B))
                self.assertEqual(hh_bump.bump(self.page), 1)
            self.assertEqual(clicks, {A: 1, B: 1})


class StateFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_pending_restart_and_account_baseline(self):
        store = Store(self.path, 14460, now=1000)
        store.set_baseline([A, B])
        store.mark_attempt(A, now=1000)
        store.mark_ambiguous(A)
        restarted = Store(self.path, 14460, now=1100)
        self.assertFalse(restarted.may_send(A, now=1100))
        self.assertTrue(restarted.may_send(A, now=15460))
        self.assertTrue(restarted.account_matches([B]))
        self.assertFalse(restarted.account_matches(["c" * 40]))
        restarted.mark_confirmed(A, now=1100)
        self.assertEqual(Store(self.path, 14460, now=1200).progress(A)["status"], "confirmed")

    def test_corrupt_state_quarantined_once(self):
        path = self.path / "runtime-state.json"
        path.write_text("{broken", encoding="utf-8")
        first = Store(self.path, 14460, now=1000)
        self.assertEqual(len(first.invalid_files), 1)
        self.assertEqual(first.due_at(now=1000), 15460)
        second = Store(self.path, 14460, now=1200)
        self.assertEqual(second.invalid_files, [])
        self.assertEqual(second.due_at(now=1200), 15460)
        self.assertEqual(len(list(self.path.glob("runtime-state.json.invalid-*"))), 1)

    def test_legacy_migration_and_short_failure_backoff(self):
        (self.path / "last-run.json").write_text(json.dumps({"time": 1000}))
        (self.path / "inspection.json").write_text(json.dumps({"resumes": [
            {"qa": "resume-card-link-" + A}, {"qa": None}]}))
        store = Store(self.path, 14460, now=20000)
        self.assertEqual(store.state["account_ids"], [A])
        store.cycle_finished(success=False, now=20000, delay=60)
        self.assertEqual(store.due_at(now=20000), 20060)

    def test_malformed_pending_deadline_never_authorizes_resend(self):
        store = Store(self.path, 14460, now=1000)
        store.mark_attempt(A, now=1000)
        raw = json.loads((self.path / "runtime-state.json").read_text())
        raw["resumes"][A]["not_before"] = True
        (self.path / "runtime-state.json").write_text(json.dumps(raw))
        recovered = Store(self.path, 14460, now=1100)
        self.assertEqual(len(recovered.invalid_files), 1)
        self.assertGreaterEqual(recovered.due_at(now=1100), 15560)

    def test_empty_baseline_backfilled_from_valid_legacy_inspection_only(self):
        initial = Store(self.path, 14460, now=1000)
        self.assertEqual(initial.state["account_ids"], [])
        (self.path / "inspection.json").write_text(json.dumps({"resumes": [
            {"qa": "resume-card-link-" + SHORT},
            {"qa": "resume-card-link-" + A},
            {"qa": 'resume-card-link-" ] button'}]}))
        recovered = Store(self.path, 14460, now=1100)
        self.assertEqual(recovered.state["account_ids"], [A, SHORT])
        recovered.set_baseline([A])
        preserved = Store(self.path, 14460, now=1200)
        self.assertEqual(preserved.state["account_ids"], [A])

    @unittest.skipUnless(os.name == "nt", "Windows byte-range lock")
    def test_second_process_reports_busy_before_reading_locked_byte(self):
        import msvcrt

        lock_path = self.path / "lock"
        command = [sys.executable, "-c",
                   "import hh_bump,sys; from pathlib import Path; "
                   "hh_bump.STATE=Path(sys.argv[1]); "
                   "sys.argv=['hh_bump.py','service']; sys.exit(hh_bump.main())",
                   str(self.path)]
        with lock_path.open("a+b") as lock:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                lock.seek(0)
                lock.write(b"0")
                lock.flush()
                second = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(second.returncode, 3, second.stderr)
                self.assertIn("already_running", second.stdout)
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        probe = subprocess.run([sys.executable, "-c",
            "import msvcrt,sys; f=open(sys.argv[1],'a+b'); f.seek(0); "
            "msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1); print('acquired')",
            str(lock_path)], capture_output=True, text=True, timeout=15)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn("acquired", probe.stdout)

    def test_service_rechecks_failed_preflight_in_same_tab_without_bump(self):
        class Clock:
            now = 1_900_000_000.0

            def time(self):
                return self.now

            def monotonic(self):
                return self.now

        clock = Clock()

        class Page:
            navigation_count = 0

            def is_closed(self):
                return clock.now >= 1_900_000_065

            def goto(self, *args, **kwargs):
                self.navigation_count += 1

            def wait_for_timeout(self, milliseconds):
                clock.now += milliseconds / 1000

        class FakeStore:
            state = {"failure_count": 0, "next_due": 2_000_000_000}

            def due_at(self, now=None):
                return 2_000_000_000

        page = Page()
        health_calls = []
        events = []
        stages = []

        def health(_):
            health_calls.append(clock.now)
            if len(health_calls) == 1:
                raise hh_bump.GlobalCycleError("Authentication required")

        with patch.object(hh_bump, "CURRENT_STORE", FakeStore()), \
             patch.object(hh_bump, "time", SimpleNamespace(time=clock.time, monotonic=clock.monotonic)), \
             patch.object(hh_bump, "service_health", side_effect=health), \
             patch.object(hh_bump, "dismiss_cookie_notice"), \
             patch.object(hh_bump, "safe_failure_artifact"), \
             patch.object(hh_bump, "record", side_effect=lambda event, **fields: events.append(event)), \
             patch.object(hh_bump, "set_stage", side_effect=lambda stage, **fields: stages.append(stage)), \
             patch.object(hh_bump, "bump") as bumped:
            self.assertEqual(hh_bump.serve(page), 0)
        self.assertEqual(page.navigation_count, 1)
        self.assertEqual(len(health_calls), 2)
        self.assertGreaterEqual(health_calls[1] - health_calls[0], 60)
        self.assertEqual(events.count("service_waiting"), 1)
        self.assertIn("needs_attention", stages)
        bumped.assert_not_called()


if __name__ == "__main__":
    unittest.main()
