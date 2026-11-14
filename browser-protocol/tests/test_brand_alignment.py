"""D-976 (Wave 5): the companion popup follows the cassini-ux brand and copy contracts.

- ux/UPSTREAM.json pins the vendored cassini-ux files by commit and SHA-256; a local edit fails.
- extension/brand-tokens.css is generated from the vendored tokens and has not drifted.
- The popup draws colours only through --cx-* roles that exist in the file (no literals), and every
  text/background pairing it draws reaches 4.5:1 in light and dark.
- The header is the cassini.talk Saturn (saturn-nav) with the lowercase wordmark (L-04, L-15).
- Copy shared with the apps is the catalog's text, verbatim.
"""
import hashlib
import html.parser
import importlib.util
import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXTENSION = ROOT / 'extension'
PIN = json.loads((ROOT / 'ux' / 'UPSTREAM.json').read_text())
TOKENS = json.loads((ROOT / 'ux' / 'brand' / 'tokens.json').read_text())
CATALOG = {entry['id']: entry for entry in json.loads((ROOT / 'ux' / 'strings' / 'strings.json').read_text())['entries']}
POPUP_HTML = (EXTENSION / 'popup.html').read_text()
POPUP_SOURCES = {name: (EXTENSION / name).read_text() for name in ('popup.html', 'popup.js', 'popup-state.mjs')}
# CSS colour keywords that would bypass the tokens (transparent and currentColor carry no colour).
NAMED = ('white', 'black', 'red', 'green', 'blue', 'yellow', 'orange', 'gray', 'grey', 'silver', 'navy', 'teal',
         'maroon', 'purple', 'fuchsia', 'lime', 'olive', 'aqua', 'canvas', 'canvastext', 'buttonface', 'linktext')
COLOUR_LITERAL = re.compile(r'#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\(|'
                            r':\s*[^;{}]*\b(?:' + '|'.join(NAMED) + r')\b', re.IGNORECASE)


def style_block(text):
    return text[text.index('<style>'):text.index('</style>')]


def generator():
    spec = importlib.util.spec_from_file_location('brand_tokens', ROOT / 'tools' / 'brand-tokens.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def luminance(hex_value):
    channels = [int(hex_value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class Header(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside, self.h1, self.images, self.text = 0, None, [], {}
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'header':
            self.inside += 1
        if self.inside and tag == 'img':
            self.images.append(attrs)
        if self.inside and tag == 'h1':
            self.current, self.h1 = 'h1', ''
        if attrs.get('id') or attrs.get('class'):
            self.current = attrs.get('id') or attrs.get('class')
            self.text.setdefault(self.current, '')

    def handle_endtag(self, tag):
        if tag == 'header':
            self.inside -= 1
        self.current = None

    def handle_data(self, data):
        if self.current == 'h1' or (self.current == 'wordmark' and self.h1 is not None):
            self.h1 += data
        if self.current:
            self.text[self.current] = self.text.get(self.current, '') + data


class VendoredPin(unittest.TestCase):
    def test_pin_names_one_cassini_ux_commit(self):
        self.assertEqual(PIN['repository'], 'codemyriad/cassini-ux')
        self.assertRegex(PIN['commit'], r'^[0-9a-f]{40}$')

    def test_vendored_copies_match_their_sha256(self):
        for upstream, entry in PIN['files'].items():
            with self.subTest(upstream=upstream):
                data = (ROOT / entry['path']).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry['sha256'],
                                 f'{entry["path"]} differs from cassini-ux {upstream}: change cassini-ux, then bump the pin')

    def test_the_mark_is_the_header_size_saturn(self):
        self.assertIn('brand/mark/saturn-nav.svg', PIN['files'])
        svg = (ROOT / PIN['files']['brand/mark/saturn-nav.svg']['path']).read_text()
        self.assertIn('viewBox="0 0 184 120"', svg)


class Tokens(unittest.TestCase):
    def test_every_read_role_has_both_modes(self):
        for name, role in TOKENS['tokens'].items():
            if role['status'] in ('decided', 'placeholder'):
                for mode in ('light', 'dark'):
                    self.assertRegex(role[mode], r'^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$', f'{name} {mode}')

    def test_generated_css_matches_the_vendored_tokens(self):
        module = generator()
        expected = module.render(TOKENS)
        self.assertEqual((EXTENSION / 'brand-tokens.css').read_text(), expected,
                         'run python3 tools/brand-tokens.py --write')
        css = expected
        light, dark = css.split('@media (prefers-color-scheme: dark)')
        for name, role in TOKENS['tokens'].items():
            if name in module.SKIP:
                self.assertNotIn(f'--cx-{name}:', css, 'desktop-only reference roles stay out of the shared extension')
            elif role['status'] in ('decided', 'placeholder'):
                self.assertIn(f'--cx-{name}:', light)
                self.assertIn(f'--cx-{name}:', dark)
                if len(role['light']) == 7:
                    self.assertRegex(light, rf'--cx-{re.escape(name)}:\s+{role["light"]};')
                    self.assertRegex(dark, rf'--cx-{re.escape(name)}:\s+{role["dark"]};')
            else:
                self.assertNotIn(f'--cx-{name}:', css, f'{role["status"]} roles are never read')

    def test_generator_converts_alpha_tokens(self):
        self.assertEqual(generator().css_colour('#00000099'), 'rgb(0 0 0 / 0.6)')

    def test_popup_loads_the_tokens_before_its_own_styles(self):
        self.assertLess(POPUP_HTML.index('href="brand-tokens.css"'), POPUP_HTML.index('<style>'))


class PopupColours(unittest.TestCase):
    def test_no_colour_literal_outside_the_token_file(self):
        for name, text in POPUP_SOURCES.items():
            checked = style_block(text) + text[text.index('</style>'):] if name.endswith('.html') else text
            for comment in re.findall(r'/\*.*?\*/', checked, re.S):
                checked = checked.replace(comment, '')
            with self.subTest(file=name):
                self.assertEqual(COLOUR_LITERAL.findall(checked), [], f'{name} draws a colour that is not a brand role')

    def test_self_test_of_the_literal_check(self):
        for bad in ('color: #fff;', 'background: rgb(1,2,3);', 'border-color: white;', 'color: hsl(1 2% 3%);'):
            self.assertTrue(COLOUR_LITERAL.search(bad), bad)
        for good in ('color: var(--cx-ink);', 'border: 1px solid transparent;', '#record { }'):
            self.assertFalse(COLOUR_LITERAL.search(good), good)

    def test_every_role_the_popup_reads_exists(self):
        used = set(re.findall(r'var\(--cx-([a-z0-9-]+)\)', POPUP_HTML))
        self.assertTrue(used)
        known = {n for n, r in TOKENS['tokens'].items() if r['status'] in ('decided', 'placeholder')} - generator().SKIP
        self.assertEqual(used - known, set())

    def test_drawn_text_pairs_reach_4_5_to_1_in_both_modes(self):
        # (text role, background role) for every text colour the popup draws on each ground it sits on.
        grounds = ('raised', 'sunken', 'success-tint', 'warn-tint')
        pairs = [(ink, ground) for ink in ('ink', 'ink-2') for ground in grounds]
        pairs += [(ink, 'raised') for ink in ('ink-3', 'success', 'warn-ink', 'error')]
        pairs += [('on-record', 'record')]
        css = style_block(POPUP_HTML)
        for ink, _ in pairs:
            self.assertTrue(ink in ('ink',) or f'var(--cx-{ink})' in css, f'{ink} is no longer drawn: update the pairs')
        for ink, ground in pairs:
            for mode in ('light', 'dark'):
                with self.subTest(ink=ink, ground=ground, mode=mode):
                    ratio = contrast(TOKENS['tokens'][ink][mode], TOKENS['tokens'][ground][mode])
                    self.assertGreaterEqual(ratio, 4.5, f'{ink} on {ground} in {mode}: {ratio:.2f}:1')

    def test_record_and_stop_use_the_record_red(self):
        css = style_block(POPUP_HTML)
        self.assertRegex(css, r'#record \{[^}]*background: var\(--cx-record\);[^}]*color: var\(--cx-on-record\);')
        self.assertNotRegex(css, r'#record\[data-action="stop"\] \{[^}]*background')

    def test_light_and_dark_come_only_from_the_token_file(self):
        self.assertIn('color-scheme: light dark', POPUP_HTML)
        self.assertNotIn('prefers-color-scheme', POPUP_HTML, 'dark values live in brand-tokens.css, not the popup')


class Wordmark(unittest.TestCase):
    def setUp(self):
        self.header = Header()
        self.header.feed(POPUP_HTML)

    def test_header_is_the_saturn_and_lowercase_wordmark(self):
        self.assertEqual(self.header.h1, 'cassini.talk')
        self.assertEqual(len(self.header.images), 1)
        mark = self.header.images[0]
        self.assertEqual(mark['src'], 'brand/saturn-nav.svg')
        self.assertEqual(mark['alt'], '', 'the wordmark names the product; the mark is decorative')
        self.assertTrue((EXTENSION / mark['src']).is_file())

    def test_wordmark_metrics(self):
        css = style_block(POPUP_HTML)
        self.assertRegex(css, r'\.mark \{[^}]*height: 26px;')
        self.assertRegex(css, r'\.wordmark \{[^}]*font: 500 18px system-ui')
        self.assertNotRegex(css, r'\.wordmark \{[^}]*text-transform: (?:capitalize|uppercase)')

    def test_old_placeholder_mark_is_gone(self):
        self.assertNotIn('<h1>Cassini</h1>', POPUP_HTML)
        self.assertNotIn('border-radius: 50%; background: #', POPUP_HTML)


class SharedCopy(unittest.TestCase):
    def setUp(self):
        self.header = Header()
        self.header.feed(POPUP_HTML)

    def test_subtitle_is_the_catalog_name_for_the_companion(self):
        self.assertEqual(self.header.text['subtitle'], CATALOG['settings.tab.browserCompanion']['en'])

    def test_privacy_promise_is_the_companion_line_verbatim(self):
        entry = CATALOG['companion.privacy']
        self.assertEqual(entry['status'], 'drawn')
        self.assertEqual(self.header.text['privacy'], entry['en'])

    def test_no_copy_contradicts_the_privacy_promise(self):
        for name, text in POPUP_SOURCES.items():
            with self.subTest(file=name):
                self.assertNotRegex(text, r'(?i)\buploaded to\b|\bsent to (?:the )?(?:cloud|server)|\bsync(?:ed|s)? to\b')


if __name__ == '__main__':
    unittest.main()
