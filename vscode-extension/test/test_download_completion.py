"""Test download completion; browser checks require Playwright and Chromium."""
import ast
import importlib.util
import os
from pathlib import Path
import unittest

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
