from pathlib import Path

from .base_tool import BaseTool
from .sanitize import sanitize_tool_output


OUTPUT_LIMIT = 10000
READ_CHUNK_SIZE = 4096


class ReadTool(BaseTool):
    def __init__(self):
        super().__init__("read_file", "Read file contents.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file to read"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of lines to read"
                    }
                },
                "required": ["path"]
            }
        )

    def run(self, path: str, limit: int = None):
        try:
            workdir = Path.cwd()
            path = (workdir / path).resolve()
            line_limit = limit if isinstance(limit, int) and limit > 0 else None
            data = bytearray()
            truncated = False
            warning = ""
            stopped_on_line_limit = False

            with path.open("rb") as f:
                while len(data) < OUTPUT_LIMIT:
                    chunk = f.read(min(READ_CHUNK_SIZE, OUTPUT_LIMIT - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if line_limit and data.count(b"\n") >= line_limit:
                        stopped_on_line_limit = True
                        break

                if len(data) >= OUTPUT_LIMIT and f.read(1):
                    truncated = True
                    warning = (
                        f"Warning: file output truncated after {len(data)} bytes; "
                        "use a narrower query, head/tail, or a smaller limit."
                    )
                elif stopped_on_line_limit and f.read(1):
                    truncated = True
                    warning = (
                        f"Warning: file output truncated after {line_limit} lines; "
                        "use a narrower query, head/tail, or a smaller limit."
                    )

            text = data.decode("utf-8", errors="replace")
            if line_limit:
                lines = text.splitlines()
                if len(lines) > line_limit:
                    text = "\n".join(lines[:line_limit])
                    truncated = True
                    warning = (
                        f"Warning: file output truncated after {line_limit} lines; "
                        "use a narrower query, head/tail, or a smaller limit."
                    )

            output = sanitize_tool_output(text)
            if truncated:
                suffix = ("\n" if output else "") + warning
                output = output[:max(0, OUTPUT_LIMIT - len(suffix))] + suffix
            return output[:OUTPUT_LIMIT]
        except Exception as e:
            return f"Error: {e}"
