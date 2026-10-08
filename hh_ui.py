"""Read HH resume controls as one bounded DOM snapshot per card."""
from __future__ import annotations

import re

from playwright.sync_api import Error as PlaywrightError
from hh_state import RESUME_ID

# Shared structural test for known offers, unknown-dialog detection, and diagnostics.
# Semantic dialog markup takes precedence; the fixed-overlay fallback also works on
# narrow viewports without classifying ordinary positioned card controls as dialogs.
MODAL_TEST_JS = r"""
const isModal = node => {
    const style = window.getComputedStyle(node);
    if (node.getClientRects().length === 0 || style.display === 'none' ||
        style.visibility === 'hidden' || style.visibility === 'collapse') return false;
    if (node.tagName === 'DIALOG' || node.getAttribute('role') === 'dialog' ||
        node.getAttribute('aria-modal') === 'true') return true;
    const marker = [node.id, typeof node.className === 'string' ? node.className : '',
        node.getAttribute('data-qa') || '']
        .join(' ').toLowerCase();
    if (/(^|[\s_-])(modal|popup|dialog)([\s_-]|$)/.test(marker)) return true;
    const rect = node.getBoundingClientRect();
    return style.position === 'fixed' &&
        rect.width >= Math.min(420, window.innerWidth * 0.3) &&
        rect.height >= Math.min(180, window.innerHeight * 0.25);
};
"""

CARD_SCRIPT = r"""key => {
    const normalize = text => (text || '').replace(/\s+/g, ' ').trim();
    const visible = el => {
        const style = window.getComputedStyle(el);
        return el.getClientRects().length > 0 && style.display !== 'none' &&
            style.visibility !== 'hidden';
    };
    const links = Array.from(document.querySelectorAll('[data-qa^="resume-card-link-"]'))
        .filter(el => el.getAttribute('data-qa') === 'resume-card-link-' + key);
    if (links.length !== 1 || !visible(links[0]))
        return {state: 'unknown', reason: 'link_missing_or_duplicate',
            link_count: links.length, visible_link_count: links.filter(visible).length};
    const diagnostic = card => {
        const ancestors = [];
        for (let node = links[0]; node && node !== document.body && ancestors.length < 8;
             node = node.parentElement) {
            const rect = node.getBoundingClientRect();
            ancestors.push({tag: node.tagName,
                class_name: (typeof node.className === 'string' ? node.className : '').slice(0, 80),
                qa: (node.getAttribute('data-qa') || '').slice(0, 80),
                link_count: node.querySelectorAll('[data-qa^="resume-card-link-"]').length,
                update_qa_count: node.querySelectorAll('[data-qa="resume-update-button"]').length,
                rect: {x: Math.round(rect.x), y: Math.round(rect.y),
                    width: Math.round(rect.width), height: Math.round(rect.height)} });
        }
        const descendant_qa = Array.from(card.querySelectorAll('[data-qa]'))
            .slice(0, 24).map(node => ({tag: node.tagName,
                role: (node.getAttribute('role') || '').slice(0, 40),
                qa: (node.getAttribute('data-qa') || '').slice(0, 80)}));
        return {ancestors, descendant_qa};
    };
    let card = links[0];
    while (card.parentElement && card.parentElement !== document.body &&
           card.parentElement.querySelectorAll('[data-qa^="resume-card-link-"]').length === 1)
        card = card.parentElement;
    if (!visible(card) || normalize(card.innerText).length > 7000)
        return {state: 'unknown', reason: 'card_boundary', link_count: links.length,
            card_tag: card.tagName, card_text_length: normalize(card.innerText).length,
            ...diagnostic(card)};
    const qaControls = Array.from(card.querySelectorAll('[data-qa="resume-update-button"]'))
        .filter(visible);
    const offered = qaControls.filter(el => normalize(el.innerText) === 'Поднять в поиске' &&
        !el.disabled && el.getAttribute('aria-disabled') !== 'true');
    const cooldown = /^Поднять в (?:[01]?\d|2[0-3]):[0-5]\d$/;
    const candidates = Array.from(card.querySelectorAll('*'))
        .filter(el => visible(el) && cooldown.test(normalize(el.innerText)));
    const cooldownLeaves = candidates.filter(el =>
        !candidates.some(other => other !== el && el.contains(other)));
    const candidateControls = Array.from(card.querySelectorAll(
        'a, button, [role="button"], [data-qa="resume-update-button"]'))
        .filter(el => visible(el) &&
            (normalize(el.innerText) === 'Поднять в поиске' ||
             cooldown.test(normalize(el.innerText))));
    if (offered.length === 1 && cooldownLeaves.length === 0 &&
        qaControls.length === 1 && qaControls[0] === offered[0])
        return {state: 'offered', reason: 'exact_action',
            control_qa: offered[0].getAttribute('data-qa') || null};
    if (cooldownLeaves.length === 1 && offered.length === 0)
        return {state: 'cooldown', reason: 'exact_time',
            control_qa: cooldownLeaves[0].getAttribute('data-qa') || null};
    const visibilityControls = Array.from(card.querySelectorAll(
        '[data-qa="resume-recommendations__button_changeVisibility"]'))
        .filter(el => visible(el) && normalize(el.innerText) === 'Сделать видимым' &&
            !el.disabled && el.getAttribute('aria-disabled') !== 'true');
    if (offered.length === 0 && cooldownLeaves.length === 0 &&
        visibilityControls.length === 1)
        return {state: 'unavailable', reason: 'visibility_control',
            control_qa: 'resume-recommendations__button_changeVisibility'};
    return {state: 'unknown', reason: 'ambiguous_or_missing_control',
        action_count: offered.length, cooldown_count: cooldownLeaves.length,
        qa_count: qaControls.length, visibility_count: visibilityControls.length,
        link_count: links.length, card_tag: card.tagName,
        ...diagnostic(card),
        candidate_controls: candidateControls.slice(0, 8).map(el => ({
            tag: el.tagName, role: (el.getAttribute('role') || '').slice(0, 40),
            qa: (el.getAttribute('data-qa') || '').slice(0, 80),
            known_label: normalize(el.innerText) === 'Поднять в поиске' ? 'offered' : 'cooldown'
        })),
        qa_controls: qaControls.slice(0, 5).map(el => ({
            tag: el.tagName, role: (el.getAttribute('role') || '').slice(0, 40),
            known_label: normalize(el.innerText) === 'Поднять в поиске' ? 'offered' :
                cooldown.test(normalize(el.innerText)) ? 'cooldown' : null,
            disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
        }))};
}"""


def resume_keys(page):
    """Collect only exact resume IDs; unrelated/null qa links are ignored."""
    values = page.locator('[data-qa^="resume-card-link-"]').evaluate_all(
        "els => els.map(el => el.getAttribute('data-qa'))")
    return list(dict.fromkeys(
        value.removeprefix("resume-card-link-")
        for value in values if isinstance(value, str) and
        RESUME_ID.fullmatch(value.removeprefix("resume-card-link-"))
    ))


def card_state(page, key):
    """Read a single coherent snapshot, with one bounded navigation/DOM retry."""
    if not RESUME_ID.fullmatch(key):
        raise ValueError("invalid resume ID")
    for attempt in range(2):
        try:
            result = page.evaluate(CARD_SCRIPT, key)
            if not isinstance(result, dict) or result.get("state") not in (
                "offered", "cooldown", "unavailable", "unknown"
            ):
                raise RuntimeError("invalid card snapshot")
            if result.get("reason") != "link_missing_or_duplicate" or attempt:
                return result
        except PlaywrightError as exc:
            transient = any(part in str(exc) for part in (
                "Execution context was destroyed", "Cannot find context",
                "Element is not attached to the DOM",
            ))
            if not transient or attempt:
                raise
        page.wait_for_timeout(250)
    raise RuntimeError("unreachable card read")


def offered_locator(page, key):
    """Live locator re-resolves the exact action on the same resume card."""
    if not RESUME_ID.fullmatch(key):
        raise ValueError("invalid resume ID")
    link = page.locator(f'[data-qa="resume-card-link-{key}"]')
    card = link.locator(
        'xpath=ancestor::*[.//*[@data-qa="resume-update-button"] and '
        'count(.//*[@data-qa and starts-with(@data-qa, '
        '"resume-card-link-")])=1][1]'
    )
    return card.locator('[data-qa="resume-update-button"]').filter(
        has_text=re.compile(r"^\s*Поднять\s+в\s+поиске\s*$"))
