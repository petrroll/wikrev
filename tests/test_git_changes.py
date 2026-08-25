from datetime import datetime, timezone
from unittest import TestCase

from wikrev.git_changes import (
    CommitInfo,
    _extract_file_diff,
    _should_exclude,
    build_change_entries,
)


def _commit(*files: str) -> CommitInfo:
    return CommitInfo(
        commit="a" * 40,
        author="Ada",
        author_email="ada@example.com",
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        subject="Update docs",
        files=list(files),
    )


class BuildChangeEntriesTests(TestCase):
    def test_ignores_files_outside_repo_prefix(self) -> None:
        commits = [_commit("Wiki/Page.md", "Service/Readme.md", "Other/Notes.md")]

        entries = build_change_entries(commits, [], "Wiki/")

        self.assertEqual([e.file_path for e in entries], ["Wiki/Page.md"])

    def test_keeps_everything_when_repo_is_git_root(self) -> None:
        commits = [_commit("Wiki/Page.md", "Other/Notes.md")]

        entries = build_change_entries(commits, [], "")

        self.assertEqual(
            [e.file_path for e in entries], ["Wiki/Page.md", "Other/Notes.md"]
        )

    def test_ignores_non_markdown_files(self) -> None:
        commits = [_commit("Wiki/Page.md", "Wiki/diagram.png", "Wiki/build.yaml")]

        entries = build_change_entries(commits, [], "Wiki/")

        self.assertEqual([e.file_path for e in entries], ["Wiki/Page.md"])

    def test_applies_path_filters_relative_to_repo_prefix(self) -> None:
        commits = [
            _commit(
                "Wiki/Release-Notes/2026-01.md",
                "Wiki/Release-Notes/Template.md",
                "Wiki/Guide.md",
            )
        ]

        entries = build_change_entries(
            commits, ["Release-Notes", "!Release-Notes/Template.md"], "Wiki/"
        )

        self.assertEqual(
            [e.file_path for e in entries],
            ["Wiki/Release-Notes/Template.md", "Wiki/Guide.md"],
        )


class ShouldExcludeTests(TestCase):
    def test_negation_overrides_folder_exclusion(self) -> None:
        filters = ["Release-Notes", "!Release-Notes/Template.md"]

        self.assertTrue(_should_exclude("Wiki/Release-Notes/2026-01.md", filters, "Wiki/"))
        self.assertFalse(_should_exclude("Wiki/Release-Notes/Template.md", filters, "Wiki/"))


class ExtractFileDiffTests(TestCase):
    DIFF = (
        "diff --git a/Wiki/A/Page.md b/Wiki/A/Page.md\n"
        "--- a/Wiki/A/Page.md\n"
        "+++ b/Wiki/A/Page.md\n"
        "@@ -1 +1 @@\n"
        "-old a\n"
        "+new a\n"
        "diff --git a/Page.md b/Page.md\n"
        "--- a/Page.md\n"
        "+++ b/Page.md\n"
        "@@ -1 +1 @@\n"
        "-old root\n"
        "+new root\n"
    )

    def test_does_not_capture_a_path_that_is_a_suffix_of_another(self) -> None:
        extracted = _extract_file_diff(self.DIFF, "Page.md")

        self.assertIn("-old root", extracted)
        self.assertNotIn("-old a", extracted)

    def test_extracts_nested_path(self) -> None:
        extracted = _extract_file_diff(self.DIFF, "Wiki/A/Page.md")

        self.assertIn("-old a", extracted)
        self.assertNotIn("-old root", extracted)

    def test_returns_empty_for_unknown_path(self) -> None:
        self.assertEqual(_extract_file_diff(self.DIFF, "Wiki/Missing.md"), "")
