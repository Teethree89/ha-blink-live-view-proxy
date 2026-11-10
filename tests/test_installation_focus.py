"""Real-browser form regressions with local fixtures; never contacts Blink/HA.

pip install playwright==1.55.0
python -m playwright install chromium webkit
python tests/test_installation_focus.py
"""

from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

FRONTEND = Path(__file__).resolve().parents[1] / "custom_components/blink_liveview_proxy/frontend"
HTML = """<!doctype html><html><body><script type="module">
import '/blink-proxy-auth-panel.js';
window.__blinkLiveviewDialogLoaded = true;
window.authState = 'idle';
window.posts = [];
window.testGets = 0;
window.panelGets = 0;
window.panel = document.createElement('blink-proxy-auth-panel');
panel._tab = 'installation';
document.body.append(panel);
panel.hass = {callApi: async (method, path, body) => {
  if (path.includes('/installation-test')) {
    if (method === 'POST') {
      posts.push(body);
      if (body.action === 'check') return {connection_verified: true};
      if (body.action === 'pin') { window.authState = 'success'; return {state: 'success', authenticated: true}; }
      throw Error('Unexpected fixture action');
    }
    testGets++;
    return {supported: true, home_assistant_installation: 'Home Assistant Container',
      test: {id: 'test-run', phase: 'ready', proxy_installation: 'systemd',
        expires_in: 1800 - Math.floor(performance.now()/1000), checks: ['health']},
      auth: {state: authState, authenticated: authState === 'success',
        challenge_id: 'test-challenge', expires_in: 900 - Math.floor(performance.now()/1000)},
      camera_count: authState === 'success' ? 6 : undefined};
  }
  if (path.endsWith('/auth/status')) return {state: 'success', authenticated: true};
  panelGets++;
  return {configured: true, title: 'Proxy', base_url: 'http://fixture.invalid',
    health: {ok: true}, versions: {integration: '1.3.0', proxy: '1.3.0', behind: false},
    status: {auth_state: 'success', uptime: panelGets}, cameras: [], update: {}, environment: {}};
}};
</script></body></html>"""


class InstallationFocusTests(unittest.TestCase):
    def test_credentials_pin_and_parent_polls_preserve_the_active_input(self):
        with sync_playwright() as playwright:
            for engine in (playwright.chromium, playwright.webkit):
                with self.subTest(browser=engine.name):
                    browser = engine.launch()
                    try:
                        page = browser.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))

                        def serve(route):
                            name = route.request.url.rsplit("/", 1)[-1]
                            if name in {"blink-proxy-auth-panel.js", "blink-installation-test.js"}:
                                route.fulfill(path=str(FRONTEND / name), content_type="text/javascript")
                            elif name == "":
                                route.fulfill(body=HTML, content_type="text/html")
                            else:
                                route.fulfill(status=404, body="")

                        page.route("http://blink.test/**", serve)
                        page.clock.install()
                        page.goto("http://blink.test/")
                        page.locator("blink-installation-test #check").click()
                        for name, text in (("username", "tester@example.invalid"), ("password", "fixture-password")):
                            field = page.locator(f'blink-installation-test input[name="{name}"]')
                            field.fill(text)
                            handle = field.element_handle()
                            page.clock.run_for(2100)
                            self.assertTrue(handle.evaluate("node => node.isConnected && node.getRootNode().activeElement === node"), name)
                            self.assertEqual(field.input_value(), text)

                        page.evaluate("async () => { authState = 'waiting_for_pin'; await panel._installationTest._refresh(); }")
                        pin = page.locator("blink-installation-test #test-pin-input")
                        pin.focus()
                        original = pin.element_handle()
                        # Type slowly through both the child 2s poll and the
                        # parent's 15s poll, including a changing countdown.
                        for digit in "123456":
                            page.keyboard.type(digit)
                            page.clock.run_for(3100)
                            self.assertTrue(original.evaluate("node => node.isConnected && node.getRootNode().activeElement === node"))
                        self.assertEqual(pin.input_value(), "123456")
                        self.assertGreater(page.evaluate("testGets"), 6)
                        self.assertGreater(page.evaluate("panelGets"), 1)
                        self.assertEqual(page.locator("blink-installation-test #test-expiry").inner_text(), "30")
                        # A visible minute change also leaves the field/caret.
                        pin.evaluate("node => node.setSelectionRange(2, 4)")
                        page.clock.run_for(61000)
                        self.assertTrue(original.evaluate("node => node.isConnected && node.getRootNode().activeElement === node"))
                        self.assertEqual(pin.evaluate("node => [node.selectionStart, node.selectionEnd]"), [2, 4])
                        self.assertEqual(page.locator("blink-installation-test #test-expiry").inner_text(), "29")
                        page.locator("blink-installation-test #test-pin button").click()
                        page.wait_for_function("authState === 'success'")
                        self.assertEqual(page.evaluate("posts.filter(p => p.action === 'pin')"), [{
                            "action": "pin", "id": "test-run", "pin": "123456", "challenge_id": "test-challenge",
                        }])
                        pin.wait_for(state="detached")
                        page.locator('blink-proxy-auth-panel button[data-tab="overview"]').click()
                        page.locator('blink-proxy-auth-panel #test-installation').click()
                        page.locator('blink-installation-test #finish').wait_for()
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()


if __name__ == "__main__":
    unittest.main()
