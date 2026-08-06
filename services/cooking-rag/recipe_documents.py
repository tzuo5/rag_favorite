from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
OBSIDIAN_EMBED_RE = re.compile(r"!\[\[([^\]]+)\]\]")
ORDER_PREFIX_RE = re.compile(r"^\d+\.\s*")
SUPPORTED_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}

MAX_SECTION_CHARACTERS = 1_200
SECTION_OVERLAP_CHARACTERS = 120


@dataclass(frozen=True)
class RecipeSection:
    section_index: int
    heading_path: str
    content: str


@dataclass(frozen=True)
class RecipeDocument:
    source_path: str
    source_sha256: str
    title: str
    cuisine: str | None
    category: str | None
    document_type: str
    content: str
    image_refs: tuple[str, ...]
    sections: tuple[RecipeSection, ...]


def discover_markdown_files(vault: Path) -> list[Path]:
    resolved = vault.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError("cooking vault does not exist or is not a directory")

    return sorted(
        path
        for path in resolved.rglob("*.md")
        if ".obsidian" not in path.parts
    )


def load_recipe_document(vault: Path, path: Path) -> RecipeDocument:
    resolved_vault = vault.expanduser().resolve()
    resolved_path = path.expanduser().resolve()

    try:
        relative = resolved_path.relative_to(resolved_vault)
    except ValueError as exc:
        raise ValueError("recipe file must remain inside the cooking vault") from exc

    raw = resolved_path.read_text(encoding="utf-8", errors="replace")
    normalized = normalize_markdown(raw)
    image_refs = tuple(dict.fromkeys(OBSIDIAN_EMBED_RE.findall(normalized)))
    searchable = OBSIDIAN_EMBED_RE.sub("", normalized).strip()
    title = resolved_path.stem.strip()
    cuisine, category, document_type = classify_path(relative)
    sections = tuple(split_markdown_sections(title, searchable))

    return RecipeDocument(
        source_path=relative.as_posix(),
        source_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        title=title,
        cuisine=cuisine,
        category=category,
        document_type=document_type,
        content=searchable,
        image_refs=image_refs,
        sections=sections,
    )


def resolve_recipe_images(
    vault: Path,
    source_path: str,
    image_refs: list[str] | tuple[str, ...] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve safe Obsidian image embeds without leaving the cooking vault."""

    resolved_vault = vault.expanduser().resolve()
    recipe_path = (resolved_vault / source_path).resolve()
    try:
        recipe_path.relative_to(resolved_vault)
    except ValueError:
        return (), tuple(image_refs or ())

    image_paths: list[str] = []
    missing_refs: list[str] = []
    seen_paths: set[Path] = set()

    for raw_ref in image_refs or ():
        normalized_ref = _normalize_image_ref(raw_ref)
        if normalized_ref is None:
            missing_refs.append(raw_ref)
            continue

        resolved = _resolve_image_ref(
            resolved_vault,
            recipe_path.parent,
            normalized_ref,
        )
        if resolved is None:
            missing_refs.append(raw_ref)
            continue
        if resolved not in seen_paths:
            seen_paths.add(resolved)
            image_paths.append(str(resolved))

    return tuple(image_paths), tuple(missing_refs)


def stage_recipe_images(
    image_paths: list[str] | tuple[str, ...],
    outbound_dir: Path,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Copy resolved recipe images into OpenClaw's managed outbound media area."""

    resolved_outbound = outbound_dir.expanduser().resolve()
    resolved_outbound.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved_outbound.chmod(0o700)

    staged_paths: list[str] = []
    failed_paths: list[str] = []
    for raw_path in image_paths:
        source = Path(raw_path).expanduser().resolve()
        if not source.is_file() or source.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            failed_paths.append(raw_path)
            continue

        digest = _sha256_file(source)
        destination = resolved_outbound / f"{digest}{source.suffix.lower()}"
        if not destination.is_file():
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=resolved_outbound,
                    prefix=".recipe-image-",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    with source.open("rb") as source_file:
                        shutil.copyfileobj(source_file, temporary)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                temporary_path.chmod(0o600)
                os.replace(temporary_path, destination)
            except OSError:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
                failed_paths.append(raw_path)
                continue
        destination.chmod(0o600)
        staged_paths.append(str(destination))

    return tuple(staged_paths), tuple(failed_paths)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_image_ref(raw_ref: str) -> Path | None:
    value = raw_ref.split("|", 1)[0].split("#", 1)[0].strip()
    if not value:
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
        return None
    return path


def _resolve_image_ref(vault: Path, recipe_dir: Path, image_ref: Path) -> Path | None:
    direct_candidates = (recipe_dir / image_ref, vault / image_ref)
    for candidate in direct_candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(vault)
        except ValueError:
            continue
        if resolved.is_file():
            return resolved

    basename_matches = [
        path.resolve()
        for path in vault.rglob(image_ref.name)
        if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
    ]
    unique_matches = list(dict.fromkeys(basename_matches))
    return unique_matches[0] if len(unique_matches) == 1 else None


def normalize_markdown(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in value.splitlines()).strip()


def derive_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        match = HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            candidate = match.group(2).strip()
            if candidate and candidate not in {"菜谱", "食材", "菜单名"}:
                return candidate
    return fallback.strip()


def classify_path(relative: Path) -> tuple[str | None, str | None, str]:
    parts = relative.parts
    if not parts:
        return None, None, "recipe"

    top = ORDER_PREFIX_RE.sub("", parts[0]).strip()
    lowered = "/".join(parts).lower()

    if top.lower().startswith("fine dining"):
        return "fine-dining", "menu-planning", "menu_plan"

    cuisine = top or None
    category = parts[1].strip() if len(parts) > 2 else None
    document_type = "menu_plan" if any(
        marker in lowered
        for marker in ("timeline", "购物清单", "shopping")
    ) else "recipe"
    return cuisine, category, document_type


def split_markdown_sections(title: str, content: str) -> list[RecipeSection]:
    if not content:
        return [RecipeSection(0, title, title)]

    heading_stack: list[tuple[int, str]] = []
    blocks: list[tuple[str, list[str]]] = []
    current_heading = title
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines
        block = "\n".join(current_lines).strip()
        if block:
            blocks.append((current_heading, block))
        current_lines = []

    for line in content.splitlines():
        match = HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            heading = match.group(2).strip()
            heading_stack[:] = [item for item in heading_stack if item[0] < level]
            heading_stack.append((level, heading))
            current_heading = " > ".join(item[1] for item in heading_stack)
        else:
            current_lines.append(line)
    flush()

    sections: list[RecipeSection] = []
    for heading_path, block in blocks:
        for chunk in bounded_chunks(block):
            sections.append(
                RecipeSection(
                    section_index=len(sections),
                    heading_path=heading_path or title,
                    content=chunk,
                )
            )

    if not sections:
        sections.append(RecipeSection(0, title, content))
    return sections


def bounded_chunks(text: str) -> list[str]:
    if len(text) <= MAX_SECTION_CHARACTERS:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + MAX_SECTION_CHARACTERS, len(text))
        if end < len(text):
            boundary = max(
                text.rfind("\n\n", start + 400, end),
                text.rfind("\n", start + 400, end),
                text.rfind("。", start + 400, end),
            )
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - SECTION_OVERLAP_CHARACTERS, start + 1)
    return chunks
