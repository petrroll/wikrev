from unittest import TestCase

from wikrev.app import _build_wiki_page_url, _render_inline_diff, _render_markdown


class MarkdownRenderingTests(TestCase):
    def test_renders_fenced_mermaid_block(self) -> None:
        rendered = _render_markdown("```mermaid\nflowchart LR\nA --> B\n```")

        self.assertIn('<code class="language-mermaid">', rendered)
        self.assertIn("flowchart LR", rendered)

    def test_renders_azure_devops_mermaid_block(self) -> None:
        rendered = _render_markdown(":::mermaid\nsequenceDiagram\nA->>B: Hello\n:::")

        self.assertIn('<code class="language-mermaid">', rendered)
        self.assertIn("sequenceDiagram", rendered)
        self.assertNotIn(":::mermaid", rendered)


class WikiPageUrlTests(TestCase):
    def test_adds_page_path_to_azure_wiki_url(self) -> None:
        url = _build_wiki_page_url(
            "https://dev.azure.com/example/docs/_wiki/wikis/Team"
            "?wikiVersion=GBmain",
            "Team/Getting%2DStarted/Overview.md",
            "Team/",
        )

        self.assertEqual(
            url,
            "https://dev.azure.com/example/docs/_wiki/wikis/Team"
            "?wikiVersion=GBmain&pagePath=%2FGetting-Started%2FOverview",
        )

    def test_supports_configured_path_placeholder(self) -> None:
        url = _build_wiki_page_url(
            "https://wiki.example/pages{path}",
            "Guides/First Page.md",
            "",
        )

        self.assertEqual(url, "https://wiki.example/pages/Guides/First%20Page")


class RichDiffTests(TestCase):
    def render(self, base: str, head: str) -> str:
        return _render_inline_diff(base, head)

    def test_added_list_item_is_its_own_block_and_keeps_numbering(self) -> None:
        rendered = self.render("1. one\n2. two\n", "1. one\n2. two\n3. three\n")

        self.assertIn('rd-added rd-bar-added', rendered)
        self.assertIn('<ol start="3">', rendered)
        self.assertEqual(rendered.count("rd-bar-"), 1)

    def test_renumbered_ordered_list_is_not_a_change(self) -> None:
        rendered = self.render("1. a\n2. b\n", "1. a\n1. b\n")

        self.assertNotIn("rd-bar-", rendered)

    def test_edited_paragraph_gets_inline_word_diff(self) -> None:
        rendered = self.render("Read it before starting.\n", "Read it before you start.\n")

        self.assertIn("rd-bar-changed", rendered)
        self.assertIn("<del>starting</del>", rendered)
        self.assertIn("<ins>you start</ins>", rendered)

    def test_pure_insertion_in_paragraph_uses_added_bar(self) -> None:
        rendered = self.render("Request a certificate.\n", "Request a certificate from PKI.\n")

        self.assertIn("rd-modified rd-bar-added", rendered)

    def test_far_unchanged_blocks_are_collapsible(self) -> None:
        base = "\n\n".join(f"Paragraph {i}." for i in range(6))
        head = base.replace("Paragraph 5.", "Paragraph five.")
        rendered = self.render(base, head)

        self.assertIn('class="rd-gap"', rendered)
        self.assertIn("unchanged blocks", rendered)
        self.assertEqual(rendered.count("rd-far"), 4)
        self.assertIn("Paragraph 0.", rendered)  # still present for the whole-document view

    def test_fenced_code_with_blank_lines_stays_one_block(self) -> None:
        code = "```\nline1\n\nline2\n```"
        rendered = self.render("Intro\n", f"Intro\n\n{code}\n")

        self.assertEqual(rendered.count("rd-added"), 1)
        self.assertIn("line1\n\nline2", rendered)

    def test_modified_table_marks_rows_not_block(self) -> None:
        table = "| A | B |\n|---|---|\n| x | 1 |\n"
        rendered = self.render(table, table + "| y | 2 |\n")

        self.assertIn('<tr class="rd-row-added">', rendered)
        self.assertNotIn("rd-bar-added", rendered)
