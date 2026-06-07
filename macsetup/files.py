from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .model import FileChange
from .ownership import (
    UserIdentity,
    context_state_owner,
    prepare_backup_root,
    repair_backup_root,
)

if TYPE_CHECKING:
    from .model import SetupContext


def expand_user(path: str | Path, home: Path) -> Path:
    text = str(path)
    if text == "~":
        return home
    if text.startswith("~/"):
        return home / text[2:]
    return Path(text)


def marker_pair(name: str, comment_prefix: str) -> tuple[str, str]:
    return (
        f"{comment_prefix} >>> macsetup:{name} >>>",
        f"{comment_prefix} <<< macsetup:{name} <<<",
    )


def managed_block(name: str, content: str, comment_prefix: str = "#") -> str:
    start, end = marker_pair(name, comment_prefix)
    body = content.strip("\n")
    return f"{start}\n{body}\n{end}\n"


def upsert_block(
    existing: str,
    *,
    name: str,
    content: str,
    comment_prefix: str = "#",
    adopt_existing_chunks: bool = False,
    adopt_existing_patterns: Sequence[str] = (),
    order: Sequence[str] = (),
) -> str:
    start, end = marker_pair(name, comment_prefix)
    if start in existing and end in existing:
        before, rest = existing.split(start, 1)
        _, after = rest.split(end, 1)
        outside = before + after
        body = (
            missing_managed_content(content, outside)
            if adopt_existing_chunks
            else content
        )
        if adopt_existing_chunks and not body.strip():
            removed = _join_without_block(before, after)
            return _reflow_managed_blocks(
                removed, order=order, comment_prefix=comment_prefix
            )
        block = managed_block(name, body, comment_prefix)
        prefix = before.rstrip("\n")
        result = (prefix + "\n" if prefix else "") + block + after.lstrip("\n")
    else:
        body = (
            missing_managed_content(
                content,
                existing,
                adopt_existing_patterns=adopt_existing_patterns,
            )
            if adopt_existing_chunks
            else content
        )
        if adopt_existing_chunks and not body.strip():
            return _reflow_managed_blocks(
                existing, order=order, comment_prefix=comment_prefix
            )
        block = managed_block(name, body, comment_prefix)
        separator = "" if not existing else "\n" if existing.endswith("\n") else "\n\n"
        result = existing + separator + block
    result = _reflow_managed_blocks(result, order=order, comment_prefix=comment_prefix)
    return result if result.endswith("\n") else result + "\n"


def _find_managed_blocks(
    existing: str, *, comment_prefix: str
) -> list[tuple[str, int, int]]:
    """Locate every macsetup-managed block in `existing`.

    Returns ``(name, start_index, end_index)`` triples where the slice
    ``existing[start_index:end_index]`` is the full block including its markers,
    ordered by appearance in the file.
    """
    prefix = re.escape(comment_prefix)
    pattern = re.compile(
        rf"^{prefix} >>> macsetup:(?P<name>.+?) >>>\n"
        rf".*?"
        rf"^{prefix} <<< macsetup:(?P=name) <<<[^\n]*\n?",
        re.MULTILINE | re.DOTALL,
    )
    found: list[tuple[str, int, int]] = []
    pos = 0
    while True:
        match = pattern.search(existing, pos)
        if match is None:
            break
        found.append((match.group("name"), match.start(), match.end()))
        pos = match.end()
    return found


def _reflow_managed_blocks(
    existing: str, *, order: Sequence[str], comment_prefix: str
) -> str:
    """Re-sort macsetup-managed blocks into canonical `order`.

    Non-managed text (user lines, other tools' content) outside the managed
    cluster is preserved. Managed blocks are lifted out and re-emitted in
    canonical order at the position where the cluster currently begins. When
    `order` is empty, the input is returned unchanged so callers opt in
    explicitly.
    """
    if not order:
        return existing
    blocks = _find_managed_blocks(existing, comment_prefix=comment_prefix)
    if len(blocks) < 2:
        return existing
    rank = {name: position for position, name in enumerate(order)}
    present = [name for name, _, _ in blocks]
    # Nothing to do when the present blocks are already in canonical order.
    ranked_present = [name for name in present if name in rank]
    if ranked_present == sorted(ranked_present, key=lambda n: rank[n]):
        return existing
    cluster_start = blocks[0][1]
    cluster_end = blocks[-1][2]
    head = existing[:cluster_start]
    tail = existing[cluster_end:]
    bodies = {name: existing[start:end] for name, start, end in blocks}
    sorted_names = sorted(
        present, key=lambda n: (rank.get(n, len(order)), present.index(n))
    )
    cluster = "\n".join(bodies[name].strip("\n") for name in sorted_names) + "\n"
    head = head.rstrip("\n")
    if head:
        head += "\n"
    tail = tail.lstrip("\n")
    separator = "\n" if head and cluster else ""
    result = head + separator + cluster + ("\n" + tail if tail else "")
    return result if result.endswith("\n") else result + "\n"


def missing_managed_content(
    content: str,
    existing: str,
    *,
    adopt_existing_patterns: Sequence[str] = (),
) -> str:
    if adopt_existing_patterns and all(
        re.search(pattern, existing, re.MULTILINE)
        for pattern in adopt_existing_patterns
    ):
        return ""
    paragraphs = _template_paragraphs(content)
    if not paragraphs:
        return content
    missing_paragraphs: list[str] = []
    adopted_any = False
    for lines in paragraphs:
        if _is_compound_shell_chunk(lines):
            chunk = "\n".join(lines)
            if _contains_chunk(existing, chunk):
                adopted_any = True
            else:
                missing_paragraphs.append(chunk)
            continue

        missing_lines = []
        for line in lines:
            if _contains_chunk(existing, line):
                adopted_any = True
            else:
                missing_lines.append(line)
        if missing_lines:
            missing_paragraphs.append("\n".join(missing_lines))

    if not adopted_any:
        return content
    return "\n\n".join(missing_paragraphs)


def _template_paragraphs(content: str) -> tuple[tuple[str, ...], ...]:
    paragraphs: list[tuple[str, ...]] = []
    paragraph: list[str] = []
    for line in content.strip("\n").splitlines():
        if line.strip():
            paragraph.append(line)
            continue
        if paragraph:
            paragraphs.append(tuple(paragraph))
        paragraph = []
    if paragraph:
        paragraphs.append(tuple(paragraph))
    return tuple(paragraphs)


def _is_compound_shell_chunk(lines: tuple[str, ...]) -> bool:
    starters = (
        "if ",
        "if [[",
        "if ((",
        "for ",
        "while ",
        "until ",
        "case ",
        "function ",
    )
    closers = {"fi", "done", "esac", "}", "else"}
    for line in lines:
        stripped = line.strip()
        if line[:1].isspace():
            return True
        if stripped in closers:
            return True
        if stripped.startswith(starters):
            return True
        if (
            stripped.endswith("{")
            or stripped.endswith("then")
            or stripped.endswith("do")
        ):
            return True
    return False


def _contains_chunk(existing: str, chunk: str) -> bool:
    body = chunk.strip("\n")
    if "\n" not in body:
        return body in existing.splitlines()
    normalized_existing = existing.strip("\n")
    return f"\n{body}\n" in f"\n{normalized_existing}\n"


def _join_without_block(before: str, after: str) -> str:
    result = before.rstrip("\n")
    trailing = after.lstrip("\n")
    if result and trailing:
        result += "\n\n"
    result += trailing
    return result if not result or result.endswith("\n") else result + "\n"


def remove_block(
    existing: str,
    *,
    name: str,
    comment_prefix: str = "#",
) -> str:
    start, end = marker_pair(name, comment_prefix)
    if start not in existing or end not in existing:
        return existing
    before, rest = existing.split(start, 1)
    _, after = rest.split(end, 1)
    return _join_without_block(before, after)


def backup_file(
    path: Path,
    backup_root: Path,
    owner: UserIdentity | None = None,
) -> Path | None:
    if not path.exists():
        return None
    stamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    absolute = path.resolve()
    relative = Path(*absolute.parts[1:]) if absolute.is_absolute() else absolute
    destination = backup_root / stamp / relative
    try:
        _copy_backup(path, destination, backup_root, owner)
    except PermissionError:
        repair_backup_root(backup_root, owner)
        _copy_backup(path, destination, backup_root, owner)
    return destination


def backup_file_for_context(path: Path, context: SetupContext) -> Path | None:
    return backup_file(path, context.backup_root, context_state_owner(context))


def _copy_backup(
    path: Path,
    destination: Path,
    backup_root: Path,
    owner: UserIdentity | None,
) -> None:
    prepare_backup_root(backup_root, owner)
    destination.parent.mkdir(parents=True, exist_ok=True)
    repair_backup_root(backup_root, owner)
    shutil.copy2(path, destination)
    repair_backup_root(backup_root, owner)


def atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.macsetup.tmp")
    temporary.write_text(content, encoding="utf-8")
    desired_mode = mode if mode is not None else current_mode(path)
    if desired_mode is not None:
        os.chmod(temporary, desired_mode)
    os.replace(temporary, path)


def current_mode(path: Path) -> int | None:
    if not path.exists():
        return None
    return stat.S_IMODE(path.stat().st_mode)


def text_file_change(path: Path, desired: str, mode: int | None = None) -> FileChange:
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    return FileChange(
        path=path,
        before=before,
        after=desired,
        existed_before=path.exists(),
        mode_before=current_mode(path),
        mode_after=mode,
    )
