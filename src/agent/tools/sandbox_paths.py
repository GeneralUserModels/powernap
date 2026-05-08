from pathlib import Path

from sandbox_runtime import SandboxManager


def resolve_path(path: str) -> Path:
    return (Path.cwd() / path).expanduser().resolve()


def assert_write_allowed(path: Path, allowed_paths: list[Path] | None = None) -> None:
    """Raise if a direct file tool would write outside the sandbox allow-list."""
    path = path.expanduser().resolve()
    if allowed_paths is None:
        config = SandboxManager.get_fs_write_config()
        allowed_paths = [Path(p) for p in (config.allow_only or [])]
    allowed_paths = [Path(p).expanduser().resolve() for p in allowed_paths]
    for allowed in allowed_paths:
        if path == allowed or allowed in path.parents:
            return
    allowed_list = ", ".join(str(p) for p in allowed_paths)
    raise PermissionError(f"Write denied outside allowed directories: {path}. Allowed: {allowed_list}")
