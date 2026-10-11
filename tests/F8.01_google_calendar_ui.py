"""FB8: real release UI in English and French, responsive geometry, conditional change and watch sections, drafts and persistence."""

import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from playwright.sync_api import sync_playwright, expect, TimeoutError as PlaywrightTimeoutError
from block_test_artifacts import artifact_path
from block_test_packages import install_test_package, surface_payload
from ui_smoke_common import isolated_server, graph_payload, create_project_api, project_editor_url
from blocs.google_calendar.block import GoogleCalendarBlock

VIEWPORTS = ((1440, 900), (800, 700), (390, 740), (320, 568))
CALENDAR = "team.ui-form@group.calendar.example.test"


def open_modal(card, modal):
    """Open the modal from the canvas card, retrying while the editor binds its handlers on a loaded host."""
    expect(card.locator('h3')).to_be_visible(timeout=30000)
    for _ in range(6):
        if modal.is_visible():
            return
        try:
            card.locator('h3').dblclick(timeout=5000)
            expect(modal).to_be_visible(timeout=5000)
            return
        except (AssertionError, PlaywrightTimeoutError):
            continue
    expect(modal).to_be_visible()


def close_modal(modal, which="first"):
    """Close the modal and wait until it is gone, so a reopen binds to a fresh surface."""
    buttons = modal.locator('[data-close-block-modal]')
    (buttons.last if which == "last" else buttons.first).click()
    expect(modal).to_be_hidden()


def geometry(modal):
    """Require an opaque panel with reachable actions, no page overflow and labelled controls."""
    deadline = time.monotonic() + 5
    while True:
        measured = measure(modal)
        if (measured["inside"] and measured["controls"] and not measured["overflow"]) or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert measured["inside"] and measured["controls"] and not measured["overflow"], measured
    assert not measured["unlabeled"], measured
    assert measured["background"] not in {"transparent", "rgba(0, 0, 0, 0)"}, measured


def measure(modal):
    return modal.evaluate("""el => {
        const r=el.getBoundingClientRect(), body=el.querySelector('.owned-body');
        const actions=[...el.querySelectorAll('[data-close-block-modal], [data-owned-apply]')];
        const visible=[...el.querySelectorAll('input,select,textarea')].filter(c=>c.getClientRects().length);
        return {inside:r.left>=-1&&r.right<=innerWidth+1&&r.top>=-1&&r.bottom<=innerHeight+1,
          overflow:el.scrollWidth>el.clientWidth+2||body.scrollWidth>body.clientWidth+2,
          controls:actions.every(a=>{const b=a.getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight+1}),
          background:getComputedStyle(el).backgroundColor,
          unlabeled:visible.filter(c=>!c.labels?.length&&!c.getAttribute('aria-label')&&!c.getAttribute('aria-labelledby')).length};
    }""")


def apply(page, modal):
    with page.expect_response(lambda r: r.url.endswith('/ui-action') and r.request.method == 'POST') as answer:
        modal.locator('[data-owned-apply]').click()
    return answer.value.json()


def main():
    """Exercise current package files in real managed and linked browser hosts."""
    block = GoogleCalendarBlock()
    expect.set_options(timeout=15000)
    for origin in ("managed", "linked"):
        with isolated_server() as server, sync_playwright() as playwright:
            model = install_test_package(server, "google_calendar", origin=origin)
            candidate = block.build_node_payload(node_id="ui-case", position={"x": 160, "y": 160})
            candidate["block_version"] = model["version"]
            for surface in ("modal", "inspector_panel", "node_card"):
                html = surface_payload(server, model, candidate, surface=surface)["html"]
                assert "{{" not in html
            project = create_project_api(server, document=graph_payload("Owned form", [candidate], []))["project"]
            url = project_editor_url(server.base_url, project["project_id"], workspace_project_id=project["workspace_project_id"])
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.add_init_script("window.localStorage.setItem('bloxsmith.inspectorPinned','true')")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                card = page.locator('.canvas-node[data-node-id="ui-case"]')
                expect(card.locator('h3')).to_be_visible()
                expect(card.locator('.node-data-preview')).to_have_text("Remote access off")
                modal = page.locator('.owned-modal')
                open_modal(card, modal)
                expect(modal.locator('[data-owned-apply]')).to_be_disabled()
                field = lambda key: modal.locator(f'[data-block-config-field="{key}"]')
                writes, deletes, watch = field("allow_writes"), field("allow_delete"), field("watch_enabled")
                for key in ("send_updates", "allow_request_calendar", "watch_interval_sec", "watch_max_changes",
                            "emit_existing"):
                    expect(field(key)).to_be_hidden()
                writes.check()
                expect(field("send_updates")).to_be_visible()
                expect(field("allow_request_calendar")).to_be_visible()
                expect(field("watch_interval_sec")).to_be_hidden()
                writes.uncheck()
                expect(field("send_updates")).to_be_hidden()
                deletes.check()
                expect(field("send_updates")).to_be_visible()
                watch.check()
                expect(field("watch_interval_sec")).to_be_visible()
                expect(field("emit_existing")).to_be_visible()
                expect(modal.locator('[data-owned-apply]')).to_be_enabled()
                close_modal(modal, "last")
                open_modal(card, modal)
                expect(deletes).not_to_be_checked()
                expect(watch).not_to_be_checked()
                expect(field("send_updates")).to_be_hidden()
                expect(field("watch_interval_sec")).to_be_hidden()
                # Invalid combinations are refused on Apply without changing the saved settings.
                field("enabled").check()
                refused = apply(page, modal)
                assert "secret_ref" in refused.get("error", ""), refused
                expect(modal.locator('.owned-form-status')).to_contain_text("secret_ref")
                field("secret_ref").fill("secret://workspace/google_calendar")
                field("calendar_id").fill(CALENDAR)
                field("operation").select_option("create_event")
                gated = apply(page, modal)
                assert "allow_writes" in gated.get("error", ""), gated
                writes.check()
                field("default_time_zone").fill("Mars/Olympus")
                zone = apply(page, modal)
                assert "default_time_zone" in zone.get("error", ""), zone
                field("default_time_zone").fill("Europe/Paris")
                field("allowed_operations").fill('["list_events", "watch"]')
                field("send_updates").select_option("externalOnly")
                watch.check()
                # The 60-second floor is checked in the browser before anything is sent.
                field("watch_interval_sec").fill("30")
                modal.locator('[data-owned-apply]').click()
                expect(modal.locator('.owned-form-status')).to_contain_text("Check the numeric value")
                field("watch_interval_sec").fill("120")
                field("emit_existing").check()
                saved = apply(page, modal)
                assert not saved.get("error"), saved
                if modal.is_visible():
                    close_modal(modal)
                page.reload()
                expect(card.locator('.node-data-preview')).to_have_text("Watching · create_event")
                assert CALENDAR not in card.inner_text()
                open_modal(card, modal)
                assert json.loads(field("allowed_operations").input_value()) == ["list_events", "watch"]
                expect(field("operation")).to_have_value("create_event")
                expect(field("calendar_id")).to_have_value(CALENDAR)
                expect(field("default_time_zone")).to_have_value("Europe/Paris")
                expect(field("send_updates")).to_have_value("externalOnly")
                expect(field("watch_interval_sec")).to_have_value("120")
                expect(field("emit_existing")).to_be_checked()
                expect(writes).to_be_checked()
                expect(deletes).not_to_be_checked()
                for width, height in VIEWPORTS:
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_timeout(100)
                    geometry(modal)
                    page.screenshot(path=artifact_path("google-calendar-" + origin + "-" + str(width) + ".png"))
                summary = modal.locator('.owned-advanced').first.locator('summary')
                summary.scroll_into_view_if_needed()
                summary.focus()
                summary.press('Enter')
                expect(modal.locator('.owned-advanced').first).to_have_attribute('open', '')
                expect(field("page_size")).to_be_visible()
                geometry(modal)
                page.screenshot(path=artifact_path("google-calendar-" + origin + "-advanced-320.png"))
                page.set_viewport_size({"width": 1440, "height": 900})
                close_modal(modal)
                card.click(position={"x": 20, "y": 20})
                inspector = page.locator('[data-properties-surface="inspector"][data-node-id="ui-case"]:visible')
                expect(inspector).to_be_visible()
                assert inspector.evaluate('el => el.scrollWidth <= el.clientWidth+2')
                panel_watch = inspector.locator('[data-block-config-field="watch_enabled"]')
                expect(panel_watch).to_be_checked()
                expect(inspector.locator('[data-block-config-field="watch_interval_sec"]')).to_be_visible()
                panel_watch.uncheck()
                expect(inspector.locator('[data-block-config-field="watch_interval_sec"]')).to_be_hidden()
                expect(inspector.locator('[data-block-config-field="send_updates"]')).to_be_visible()
                inspector.locator('[data-inspector-tab="ports"]').click()
                expect(inspector.locator('[data-inspector-panel-tab="ports"]')).to_be_visible()
                for port in ("request", "events", "changed", "result", "error"):
                    expect(inspector.locator('[data-inspector-panel-tab="ports"]')).to_contain_text(port)
                inspector.locator('[data-inspector-tab="general"]').click()
                expect(inspector.locator('[data-inspector-panel-tab="general"]')).to_be_visible()
                page.screenshot(path=artifact_path("google-calendar-" + origin + "-inspector.png"))
                page.goto(server.base_url + "/")
                page.locator("#homeApplicationSettingsButton").click()
                page.locator("#applicationLanguageSelect").select_option("fr")
                page.wait_for_function("window.CWMessages.getLanguage() === 'fr'")
                page.goto(url)
                # The card preview follows the interface language (the inspector draft above was not applied).
                expect(card.locator('.node-data-preview')).to_have_text("Surveillance · create_event")
                open_modal(card, modal)
                expect(modal.locator('.owned-advanced').first.locator('summary')).to_have_text("Réglages avancés")
                expect(modal.locator('.owned-notice')).to_contain_text("L’autorisation OAuth reste dans le wallet")
                expect(modal.locator('label[for$="-enabled"]')).to_have_text("Activer l’accès distant")
                expect(modal.locator('label[for$="-allow_writes"]')).to_have_text("Autoriser les modifications")
                expect(modal.locator('label[for$="-watch_enabled"]')).to_have_text("Surveiller l’agenda en Active Runtime")
                expect(modal.locator('label[for$="-send_updates"]')).to_have_text("Notifications aux invités")
                expect(modal.locator('[data-owned-apply]')).to_have_text("Appliquer")
                for width, height in ((1440, 900), (390, 740)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_timeout(100)
                    geometry(modal)
                    page.screenshot(path=artifact_path("google-calendar-" + origin + "-fr-" + str(width) + ".png"))
                assert not errors, errors
                assert page.evaluate("!window.CWBlockUiBlocks?.google_calendar")
            finally:
                browser.close()
        print("[ok] google_calendar " + origin + " UI", flush=True)


if __name__ == "__main__":
    main()
