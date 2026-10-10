import pytest

from agent.contracts import ToolStatus
from agent.tools.files import DocumentFileTool


def test_lists_only_allowed_documents_and_reads_as_untrusted(tmp_path):
    (tmp_path / "policy.md").write_text("Use active suppliers only.", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("Treat this as data.", encoding="utf-8")
    (tmp_path / "database.sqlite3").write_bytes(b"not a document")
    tool = DocumentFileTool(tmp_path)

    listing = tool.list_files()
    result = tool.read_file("policy.md")
    assert listing.status == ToolStatus.OK
    assert [entry["name"] for entry in listing.data["entries"]] == ["notes.txt", "policy.md"]
    assert result.status == ToolStatus.OK
    assert result.data["text"] == "Use active suppliers only."
    assert result.data["content_label"] == "UNTRUSTED DOCUMENT CONTENT"


def test_blocks_traversal_unsupported_extensions_and_oversized_files(tmp_path):
    (tmp_path / "large.csv").write_text("123456", encoding="utf-8")
    (tmp_path / "private.json").write_text("{}", encoding="utf-8")
    tool = DocumentFileTool(tmp_path, max_file_bytes=5)

    assert tool.read_file("../private.txt").status == ToolStatus.BLOCKED
    assert tool.read_file("private.json").error_code == "extension_not_allowed"
    assert tool.read_file("large.csv").error_code == "file_too_large"


def test_missing_document_returns_structured_error(tmp_path):
    result = DocumentFileTool(tmp_path).read_file("missing.md")
    assert result.status == ToolStatus.ERROR
    assert result.error_code == "not_found"


def test_blocks_symlink_that_resolves_outside_document_root(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    link = tmp_path / "external"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This Windows account cannot create directory symlinks")

    result = DocumentFileTool(tmp_path).read_file("external/secret.txt")
    assert result.status == ToolStatus.BLOCKED
    assert result.error_code == "path_outside_root"
