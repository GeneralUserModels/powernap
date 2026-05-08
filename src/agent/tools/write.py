from .base_tool import BaseTool
from .sandbox_paths import assert_write_allowed, resolve_path


class WriteTool(BaseTool):
    def __init__(self, allowed_paths=None):
        self.allowed_paths = allowed_paths
        super().__init__("write_file", "Write content to file.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file to write"
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write to the file"
                    }
                },
                "required": ["path", "content"]
            }
        )

    def run(self, path: str, content: str):
        resolved = resolve_path(path)
        assert_write_allowed(resolved, self.allowed_paths)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
