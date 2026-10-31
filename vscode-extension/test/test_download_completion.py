"""Test download completion; browser checks require Playwright and Chromium."""
import ast
import importlib.util
import os
from pathlib import Path
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'copilot_repo_roundtrip.py'
spec = importlib.util.spec_from_file_location('roundtrip', SCRIPT)
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)


class CompletionTests(unittest.TestCase):
    def test_prompts_include_repository_maintenance_instructions(self):
        for template in (rt.BASE_PROMPT, rt.FOLLOWUP_PROMPT):
            with self.subTest(template=template):
                prompt = template.format(request='Update the tests')
                self.assertIn('AGENTS.md', prompt)
                self.assertIn('.github/copilot-instructions.md', prompt)
                self.assertIn('README', prompt)
                self.assertIn('comments in all repository files', prompt)
                self.assertIn('no historical references', prompt)
                self.assertTrue(prompt.endswith('To fix: Update the tests'))

    def test_zip_labels_without_filename(self):
        for label in ('Download the complete fixed repository ZIP', 'repo-fixed.zip'):
            self.assertRegex(label, rt.ZIP_NAME)
        for label in ('repo.zip.txt', 'zipper', 'gzip'):
            self.assertNotRegex(label, rt.ZIP_NAME)

    def test_download_retries_remain_available_after_six_attempts(self):
        self.assertTrue(rt.download_retry_due(0, 0, 0))
        self.assertFalse(rt.download_retry_due(1, 100, 109))
        self.assertTrue(rt.download_retry_due(1, 100, 110))
        for count in (6, 7, 100):
            self.assertFalse(rt.download_retry_due(count, 100, 159))
            self.assertTrue(rt.download_retry_due(count, 100, 160))

    def test_download_precedes_optional_reasoning_capture(self):
        tree = ast.parse(SCRIPT.read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_roundtrip')
        loop = next(n for n in ast.walk(fn) if isinstance(n, ast.While) and ast.unparse(n.test) == 'downloaded is None')
        calls = {name: min(n.lineno for n in ast.walk(loop) if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Name) and n.func.id == name)
                 for name in ('download_candidates', 'read_screen')}
        self.assertLess(calls['download_candidates'], calls['read_screen'])
        self.assertTrue(any(isinstance(n, ast.If) and ast.unparse(n.test) == 'downloaded is not None'
                            and any(isinstance(b, ast.Break) for b in n.body)
                            and calls['download_candidates'] < n.lineno < calls['read_screen']
                            for n in loop.body))


class DownloadFlowTests(unittest.TestCase):
    def setUp(self):
        self.page = Mock(url='https://chat.test/conversation')
        self.page.locator.return_value.locator.return_value.count.return_value = 0
        self.ctx = Mock(pages=[self.page])
        self.listeners = {}
        self.page.on.side_effect = lambda event, callback: self.listeners.update({event: callback})
        self.download = Mock(suggested_filename='fixed.zip')
        self.node = Mock()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def download_now(self):
        self.listeners['download'](self.download)

    def test_download_survives_interrupted_click(self):
        def click(**kwargs):
            self.download_now()
            raise RuntimeError('Download navigation interrupted')
        self.node.click.side_effect = click
        self.assertEqual(rt.click_download(self.ctx, self.page, self.node, self.work),
                         self.work / 'fixed.zip')
        self.download.save_as.assert_called_once_with(str(self.work / 'fixed.zip'))
        self.page.remove_listener.assert_called_once()
        self.ctx.remove_listener.assert_called_once()

    def test_preview_downloads_and_restores_chat(self):
        for popup in (False, True):
            with self.subTest(popup=popup):
                self.setUp()
                preview = Mock(url='https://chat.test/preview') if popup else self.page
                control = Mock()
                control.is_visible.return_value = True
                control.is_enabled.return_value = True
                control.get_attribute.return_value = 'Download'
                control.inner_text.return_value = 'Download'
                controls = Mock()
                controls.count.return_value = 1
                controls.nth.return_value = control
                preview.locator.return_value = controls
                def open_preview(**kwargs):
                    if popup:
                        self.ctx.pages.append(preview)
                    else:
                        self.page.url = 'https://chat.test/preview'
                self.node.click.side_effect = open_preview
                def download(**kwargs):
                    callback = next(call.args[1] for call in preview.on.call_args_list
                                    if call.args[0] == 'download')
                    callback(self.download)
                control.click.side_effect = download
                self.assertEqual(rt.click_download(self.ctx, self.page, self.node, self.work),
                                 self.work / 'fixed.zip')
                if popup:
                    preview.close.assert_called_once()
                    self.page.go_back.assert_not_called()
                else:
                    self.page.go_back.assert_called_once()

    def test_dialog_download(self):
        control = Mock()
        controls = self.page.locator.return_value.locator.return_value
        controls.count.return_value = 1
        controls.nth.return_value = control
        control.get_attribute.return_value = 'Download'
        control.inner_text.return_value = 'Download'
        control.click.side_effect = lambda **kwargs: self.download_now()
        self.assertEqual(rt.click_download(self.ctx, self.page, self.node, self.work),
                         self.work / 'fixed.zip')

    def test_cancellation_is_not_swallowed(self):
        self.node.click.side_effect = rt.QuitRequested()
        with self.assertRaises(rt.QuitRequested):
            rt.click_download(self.ctx, self.page, self.node, self.work)
        self.download.save_as.assert_not_called()
        self.ctx.remove_listener.assert_called_once()

    def test_no_download_returns_none_and_restores_chat(self):
        self.node.click.side_effect = lambda **kwargs: setattr(self.page, 'url', 'https://chat.test/preview')
        self.assertIsNone(rt.click_download(self.ctx, self.page, self.node, self.work, timeout_seconds=0))
        self.page.go_back.assert_called_once()
        self.download.save_as.assert_not_called()


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise unittest.SkipTest(str(e))
        cls.pw = sync_playwright().start()
        try:
            options = {'headless': True}
            if os.environ.get('ROUNDTRIP_TEST_CHROMIUM'):
                options['executable_path'] = os.environ['ROUNDTRIP_TEST_CHROMIUM']
            cls.browser = cls.pw.chromium.launch(**options)
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(str(e))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.sel = rt.selectors_from_env()

    def test_descriptive_zip_link_with_opaque_target(self):
        self.page.set_content('<a href="/artifact/123">Download the complete fixed repository ZIP</a>')
        found = rt.download_candidates(self.page, self.sel, [])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], '/artifact/123')

    def test_prior_uploaded_hidden_and_unrelated_links_excluded(self):
        self.page.set_content('''<a data-roundtrip-prior="1" href="/old">Previous ZIP</a>
          <a href="/upload">repo.zip.txt</a><a href="/unrelated">zipper</a>
          <a hidden href="/hidden">Result ZIP</a>
          <a href="/new">Fixed repository ZIP</a>''')
        found = rt.download_candidates(self.page, self.sel, ['repo.zip.txt'])
        self.assertEqual([x[1] for x in found], ['/new'])

    def test_finished_coding_only_section_needs_no_reasoning_subsection(self):
        self.page.set_content('''<main><article data-message-author-role="assistant">
          <details><summary>Reasoned for 18s</summary>
            <div>Coding and executing<pre>secret_code()</pre></div>
          </details><p>Done.</p>
          <a href="/artifact/123">Download the complete fixed repository ZIP</a>
          </article></main>''')
        _, blocks, answers = rt.read_screen(self.page, self.sel)
        self.assertEqual(len(blocks), 1)
        self.assertFalse(blocks[0]['active'])
        self.assertEqual(blocks[0]['steps'], [{'title': 'Coding and executing', 'total': 0, 'lines': []}])
        self.assertNotIn('secret_code', str((blocks, answers)))
        self.assertEqual(len(rt.download_candidates(self.page, self.sel, [])), 1)
        self.assertTrue(rt.wait_for_response_end(self.page, self.sel, timeout_seconds=1))


if __name__ == '__main__':
    unittest.main()
