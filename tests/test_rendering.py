from unittest import TestCase

from wikrev.app import _build_wiki_page_url, _render_markdown


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
