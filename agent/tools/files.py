"""Read-only, size-limited access to the business documents folder."""

from pathlib import Path

from agent.contracts import ToolResult, ToolStatus


DOCUMENT_ROOT = Path(__file__).resolve().parents[2] / "env" / "data"
ALLOWED_EXTENSIONS = {".md", ".txt", ".csv"}


class DocumentFileTool:
    def __init__(self, root: str | Path = DOCUMENT_ROOT, max_file_bytes: int = 32_768):
        self.root = Path(root).resolve()
        self.max_file_bytes = max_file_bytes

    def _resolve(self, relative_path: str | Path) -> Path:
        requested = Path(relative_path)
        if requested.is_absolute() or ".." in requested.parts:
            raise PermissionError("path must stay inside the documents folder")
        root = self.root.resolve(strict=True)
        candidate = (root / requested).resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise PermissionError("path must stay inside the documents folder")
        return candidate

    def list_files(self, directory: str = ".") -> ToolResult:
        try:
            folder = self._resolve(directory)
            if not folder.is_dir():
                return ToolResult(ToolStatus.ERROR, error_code="not_a_directory", message="Requested path is not a folder.")
            entries = []
            for child in folder.iterdir():
                try:
                    resolved = child.resolve(strict=True)
                    if not resolved.is_relative_to(self.root):
                        continue
                    if resolved.is_dir():
                        entries.append({"name": child.name, "type": "directory", "size_bytes": None})
                    elif resolved.suffix.casefold() in ALLOWED_EXTENSIONS:
                        entries.append({"name": child.name, "type": "file", "size_bytes": resolved.stat().st_size})
                except OSError:
                    continue
            entries.sort(key=lambda item: item["name"].casefold())
            return ToolResult(ToolStatus.OK, data={"directory": directory, "entries": entries})
        except PermissionError as exc:
            return ToolResult(ToolStatus.BLOCKED, error_code="path_outside_root", message=str(exc))
        except FileNotFoundError:
            return ToolResult(ToolStatus.ERROR, error_code="not_found", message="Folder does not exist.")
        except OSError as exc:
            return ToolResult(ToolStatus.ERROR, error_code="file_error", message=str(exc))

    def read_file(self, relative_path: str) -> ToolResult:
        try:
            file_path = self._resolve(relative_path)
            if not file_path.is_file():
                return ToolResult(ToolStatus.ERROR, error_code="not_a_file", message="Requested path is not a file.")
            if file_path.suffix.casefold() not in ALLOWED_EXTENSIONS:
                return ToolResult(ToolStatus.BLOCKED, error_code="extension_not_allowed", message="Only .md, .txt, and .csv documents can be read.")
            if file_path.stat().st_size > self.max_file_bytes:
                return ToolResult(ToolStatus.BLOCKED, error_code="file_too_large", message="Document exceeds the configured size limit.")
            text = file_path.read_text(encoding="utf-8")
            return ToolResult(
                ToolStatus.OK,
                data={"path": relative_path, "text": text, "content_label": "UNTRUSTED DOCUMENT CONTENT"},
            )
        except PermissionError as exc:
            return ToolResult(ToolStatus.BLOCKED, error_code="path_outside_root", message=str(exc))
        except FileNotFoundError:
            return ToolResult(ToolStatus.ERROR, error_code="not_found", message="Document does not exist.")
        except UnicodeDecodeError:
            return ToolResult(ToolStatus.ERROR, error_code="invalid_encoding", message="Document must be UTF-8 text.")
        except OSError as exc:
            return ToolResult(ToolStatus.ERROR, error_code="file_error", message=str(exc))
