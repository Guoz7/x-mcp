"""Static checks for ui/composer.html (MCP Apps composer workbench).

These tests read the shipped HTML as text. They do not render it, so they prove
the presence of protocol hooks, not that a host displays the view correctly.
"""

from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "ui" / "composer.html"
MAX_BYTES = 60 * 1024


class _Collector(HTMLParser):
    """Collect tag names plus text found inside script/style blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.script_text: list[str] = []
        self.style_text: list[str] = []
        self._mode: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        if tag in ("script", "style"):
            self._mode = tag

    def handle_endtag(self, tag: str) -> None:
        if tag == self._mode:
            self._mode = None

    def handle_data(self, data: str) -> None:
        if self._mode == "script":
            self.script_text.append(data)
        elif self._mode == "style":
            self.style_text.append(data)


class WorkbenchUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = HTML_PATH.read_bytes()
        cls.text = cls.raw.decode("utf-8")
        cls.parsed = _Collector()
        cls.parsed.feed(cls.text)
        cls.parsed.close()
        cls.script = "".join(cls.parsed.script_text)
        cls.style = "".join(cls.parsed.style_text)

    def test_file_exists_and_is_small(self) -> None:
        self.assertTrue(HTML_PATH.is_file(), f"missing {HTML_PATH}")
        self.assertLess(len(self.raw), MAX_BYTES, "composer.html must stay under 60 KB")

    def test_parses_completely_with_script_and_style(self) -> None:
        self.assertIn("html", self.parsed.tags)
        self.assertIn("body", self.parsed.tags)
        self.assertGreaterEqual(self.parsed.tags.count("script"), 1)
        self.assertGreaterEqual(self.parsed.tags.count("style"), 1)
        self.assertIn("</script>", self.text)
        self.assertIn("</style>", self.text)
        self.assertTrue(self.script.strip(), "script block is empty")
        self.assertTrue(self.style.strip(), "style block is empty")

    def test_initialize_handshake_present(self) -> None:
        self.assertIn("ui/initialize", self.script)
        self.assertIn("ui/notifications/initialized", self.script)
        self.assertIn('"x-starter-composer"', self.script)
        self.assertRegex(self.script, r'availableDisplayModes\s*:\s*\[\s*"inline"')
        self.assertIn('"fullscreen"', self.script)

    def test_initialize_sent_through_parent_postmessage(self) -> None:
        self.assertIn("window.parent.postMessage", self.script)
        self.assertIn('jsonrpc: "2.0"', self.script)
        init_index = self.script.index('request("ui/initialize"')
        notify_index = self.script.index('notify("ui/notifications/initialized"')
        self.assertLess(init_index, notify_index, "initialized must follow initialize")

    def test_handles_tool_input_and_tool_result(self) -> None:
        self.assertIn("ui/notifications/tool-input", self.script)
        self.assertIn("ui/notifications/tool-result", self.script)
        self.assertIn("structuredContent", self.script)
        self.assertIn("JSON.parse", self.script)

    def test_handles_host_context_theme_and_locale(self) -> None:
        self.assertIn("ui/notifications/host-context-changed", self.script)
        self.assertIn("hostContext", self.script)
        self.assertRegex(self.script, r'theme\s*===?\s*"dark"|ctx\.theme')
        self.assertIn("/^zh/i", self.script)

    def test_request_ids_carry_timeout(self) -> None:
        self.assertIn("HOST_TIMEOUT", self.script)
        self.assertIn("10000", self.script)
        self.assertIn("delete pending[", self.script)
        self.assertIn("reject(new Error(\"timeout\"))", self.script)

    def test_calls_compose_check_tool(self) -> None:
        self.assertIn("x_compose_check", self.script)
        self.assertIn('request("tools/call"', self.script)
        self.assertIn('arguments: { text: text }', self.script)
        self.assertIn("400", self.script)

    def test_counts_weighted_length_locally(self) -> None:
        self.assertIn("weightedLength", self.script)
        self.assertIn("23", self.script)
        self.assertIn("Extended_Pictographic", self.script)
        self.assertIn("4351", self.script)

    def test_open_link_and_message_and_size(self) -> None:
        self.assertIn("ui/open-link", self.script)
        self.assertIn("ui/message", self.script)
        self.assertIn("ui/notifications/size-changed", self.script)
        self.assertIn("ResizeObserver", self.script)
        self.assertIn("Rewrite this X post draft:", self.script)
        self.assertIn('role: "user"', self.script)

    def test_intent_url_built_and_allowlisted(self) -> None:
        self.assertIn("https://x.com/intent/tweet", self.script)
        self.assertIn("encodeURIComponent", self.script)
        self.assertIn("https://x.com/intent/", self.script)
        self.assertIn("indexOf(\"https://x.com/intent/\")", self.script)
        self.assertIn('window.open(url, "_blank", "noopener")', self.script)

    def test_no_plain_http_transport(self) -> None:
        self.assertNotIn("http://", self.text)

    def test_no_external_subresources(self) -> None:
        for match in re.finditer(r"<(?:script|img|link|iframe)\b[^>]*>", self.text, re.I):
            tag = match.group(0)
            self.assertNotRegex(tag, r'(?:src|href)\s*=\s*["\'](?:https?:)?//', tag)
        self.assertNotIn("<link", self.text.lower())
        self.assertNotIn("@import", self.style)
        self.assertNotIn('url("http', self.style)

    def test_no_innerhtml_assignment(self) -> None:
        self.assertNotIn("innerHTML =", self.text)
        self.assertNotIn("innerHTML=", self.text)
        self.assertNotIn("outerHTML", self.text)
        self.assertNotIn("document.write", self.text)

    def test_demo_mode_for_standalone_preview(self) -> None:
        self.assertIn("window.parent === window", self.script)
        self.assertIn("Demo data", self.text)
        self.assertIn("function startPreview", self.script)

    def test_accessibility_and_motion_preferences(self) -> None:
        self.assertIn("prefers-reduced-motion", self.style)
        self.assertIn("aria-label", self.text)
        self.assertIn(":focus-visible", self.style)
        self.assertIn('aria-live="polite"', self.text)
        self.assertIn('aria-busy', self.script)

    def test_icons_are_inline_svg_with_license_note(self) -> None:
        self.assertIn("<svg", self.text)
        self.assertIn("Lucide", self.text)
        self.assertIn("ISC", self.text)
        self.assertIn('stroke="currentColor"', self.text)
        # No pictographic glyphs may stand in for icons; CJK copy is fine.
        emoji = re.findall(
            "[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]",
            self.text,
        )
        self.assertEqual(emoji, [], f"emoji glyphs in markup: {emoji}")

    def test_palette_uses_blue_accent_and_red_for_errors(self) -> None:
        self.assertIn("#1d4ed8", self.style)
        self.assertIn("--danger", self.style)
        self.assertIn("--ok", self.style)
        self.assertNotIn("linear-gradient(135deg, #7c3aed", self.style)

    def test_bilingual_copy_table_exists(self) -> None:
        self.assertIn("en:", self.script)
        self.assertIn("zh:", self.script)
        self.assertIn("在 X 中打开并发布", self.script)
        self.assertIn("Open in X and post", self.script)

    def test_fixed_buttons_have_hover_press_and_disabled_rules(self) -> None:
        self.assertRegex(self.style, r"\.btn:hover:not\(:disabled\)[^{]*\{[^}]*scale\(1\.02\)")
        self.assertRegex(self.style, r"\.btn:active:not\(:disabled\)[^{]*\{[^}]*scale\(\.98\)")
        self.assertIn(".btn:disabled", self.style)
        self.assertIn(".btn.busy", self.style)
        self.assertIn(".btn.done", self.style)

    def test_builds_dom_with_text_content(self) -> None:
        self.assertIn("textContent", self.script)
        self.assertIn("createElement", self.script)
        self.assertIn("clear(node)", self.script)


if __name__ == "__main__":
    unittest.main()
