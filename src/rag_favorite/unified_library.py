"""Versioned summary retrieval for documents of every origin, without topic routing."""

from __future__ import annotations

import json
import re
from uuid import UUID

from pgvector import Vector
from psycopg.types.json import Jsonb

from .config import ConfigError
from .database import connect_database
from .embedding import client_from_config, embedding_space_id
from .indexes import resolve_index
from .knowledge_summaries import atomic_text, summary_chunks, text_hash
from .title_dedup import title_text
from .video_store import library_id


def knowledge_root(config):
    return config.collection("general").path


def retrieval_text(summary):
    """Separate display metadata from knowledge. Never embed topic/source annotations."""
    text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", summary, flags=re.DOTALL)
    return (
        "\n".join(
            line
            for line in text.splitlines()
            if not re.match(
                r"^\s*(?:文档类型|内容类型|类型|分类|标签|tags?|topics?|来源|Source|source|原始链接)\s*[:：]",
                line,
            )
        ).strip()
        + "\n"
    )


def active_generation(config, video):
    with connect_database(config, register_pgvector=False) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        row = c.execute(
            "SELECT active_generation FROM public.rag_library_state WHERE library_id=%s",
            (library_id(video),),
        ).fetchone()
    if not row:
        raise ConfigError("Unified library has no active generation.")
    return row[0]


def create_generation(config, video, generation):
    space = embedding_space_id(resolve_index(config).embedding)
    with connect_database(config) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        c.execute(
            "INSERT INTO public.rag_library_generations(library_id,generation,embedding_space_id) "
            "VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
            (library_id(video), generation, space),
        )
        row = c.execute(
            "SELECT embedding_space_id FROM public.rag_library_generations WHERE library_id=%s AND generation=%s",
            (library_id(video), generation),
        ).fetchone()
        if row[0] != space:
            raise ConfigError("Generation uses a different embedding space.")


def register_document(
    config,
    video,
    doc_id,
    draft,
    title,
    source,
    metadata,
    *,
    source_kind="markdown",
    source_job_id=None,
):
    doc_id = str(UUID(str(doc_id)))
    title_key = (
        title_text(title, legacy_filename=bool(metadata.get("source_paths")))
        if (source_kind == "media" or source_job_id or metadata.get("source_urls"))
        else ""
    )
    configured_root = knowledge_root(config)
    root = configured_root.resolve()
    directory = root / doc_id
    if configured_root.is_symlink() or directory.is_symlink():
        raise ConfigError("Unsafe knowledge directory.")
    atomic_text(directory / "knowledge.md", draft)
    atomic_text(
        directory / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2)
    )
    with connect_database(config) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        c.execute(
            """INSERT INTO public.rag_knowledge_documents
          (id,library_id,source_kind,title,source_label,draft,draft_sha256,metadata,source_job_id,title_key)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET
          title=excluded.title,source_label=excluded.source_label,draft=excluded.draft,
          draft_sha256=excluded.draft_sha256,metadata=excluded.metadata,title_key=excluded.title_key,updated_at=now() WHERE rag_knowledge_documents.library_id=excluded.library_id RETURNING id""",
            (
                doc_id,
                library_id(video),
                source_kind,
                title,
                source,
                draft,
                text_hash(draft),
                Jsonb(metadata),
                source_job_id,
                title_key,
            ),
        )
    return doc_id


def queue_summary(config, video, generation, doc_id):
    with connect_database(config) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        c.execute(
            "INSERT INTO public.rag_document_summary_jobs(library_id,generation,document_id) "
            "VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
            (library_id(video), generation, doc_id),
        )


def extractive_summary(draft, title):
    """Bounded source excerpts with paragraph provenance; never invent source claims."""
    paragraphs = re.split(r"\n\s*\n", retrieval_text(draft))
    tokens = {title[i : i + 2] for i in range(max(0, len(title) - 1))}
    candidates = []
    headings = []
    for ordinal, paragraph in enumerate(paragraphs):
        paragraph = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", paragraph).strip()
        if not paragraph or paragraph.startswith("<!--"):
            continue
        sentences = []
        for line in paragraph.splitlines():
            if re.match(r"^#{1,6}\s+", line):
                if line.lstrip("# ").strip() != title:
                    headings.append((ordinal, line[:120]))
                continue
            sentences.extend(re.split(r"(?<=[。！？])\s*|(?<=[.!?])\s+", line))
        for index, sentence in enumerate(sentences):
            sentence = sentence.strip()
            if not sentence:
                continue
            # An unusually long punctuation-free source line is explicitly an
            # excerpt. The complete original remains available via document_read.
            if len(sentence) > 850:
                sentence = sentence[:850] + "〔原文摘录，后文请读取原文〕"
            score = (4 if index == 0 else 0) + min(
                3, sum(t in sentence for t in tokens) * 0.1
            )
            if re.search(
                r"不确定|冲突|unknown|conflict|uncertain", sentence, re.IGNORECASE
            ):
                score += 4
            if re.search(r"\d", sentence):
                score += 1
            candidates.append((score, ordinal, index, sentence))
    if not candidates:
        raise ConfigError("SUMMARY_NO_TEXT_EVIDENCE")
    selected = []
    remaining = 3300
    for _, ordinal, index, sentence in sorted(
        candidates, key=lambda x: (-x[0], x[1], x[2])
    ):
        item = sentence + f"\n〔原文段落 {ordinal}〕"
        if len(item) > remaining:
            continue
        selected.append((ordinal, index, item))
        remaining -= len(item) + 2
    if not selected:
        raise ConfigError("SUMMARY_NO_TEXT_EVIDENCE")
    for ordinal, heading in headings:
        if len(heading) + 2 <= remaining:
            selected.append((ordinal, -1, heading))
            remaining -= len(heading) + 2
    return "# " + title + "\n\n" + "\n\n".join(x[2] for x in sorted(selected)) + "\n"


def publish_summary(config, video, generation, doc_id, summary, *, encoder=None):
    profile = resolve_index(config)
    space = embedding_space_id(profile.embedding)
    with connect_database(profile) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        row = c.execute(
            "SELECT title,draft,draft_sha256,metadata FROM public.rag_knowledge_documents "
            "WHERE id=%s AND library_id=%s",
            (doc_id, library_id(video)),
        ).fetchone()
        if not row:
            raise ConfigError("Unknown knowledge document.")
        existing = c.execute(
            "SELECT draft_sha256,summary_sha256,embedding_space_id FROM public.rag_document_summaries "
            "WHERE library_id=%s AND generation=%s AND document_id=%s",
            (library_id(video), generation, doc_id),
        ).fetchone()
    search = retrieval_text(summary)
    sha = text_hash(summary)
    if existing == (row[2], sha, space):
        # Repair the durable task/file if a prior run stopped after publication.
        with connect_database(profile) as c:
            from .pipeline_fence import assert_owner

            assert_owner(c)
            c.execute(
                "UPDATE public.rag_document_summary_jobs SET state='complete',error_code=NULL,updated_at=now() "
                "WHERE library_id=%s AND generation=%s AND document_id=%s",
                (library_id(video), generation, doc_id),
            )
        atomic_text(knowledge_root(config) / str(doc_id) / "summary.md", summary)
        return {"state": "complete", "document_id": str(doc_id), "cached": True}
    encoder = encoder or client_from_config(profile)
    chunks = summary_chunks(search, encoder)
    from .pipeline_fence import embedding_slot

    with embedding_slot(video):
        vectors = encoder.embed_documents(chunks)
    if (
        not chunks
        or len(vectors) != len(chunks)
        or any(len(v) != profile.embedding.dimensions for v in vectors)
    ):
        raise ConfigError("Invalid unified summary embeddings.")
    version = text_hash(row[2] + sha + text_hash(search))
    with connect_database(profile) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", ("knowledge:" + str(doc_id),)
        )
        current = c.execute(
            "SELECT draft_sha256 FROM public.rag_knowledge_documents WHERE id=%s",
            (doc_id,),
        ).fetchone()
        if not current or current[0] != row[2]:
            raise ConfigError("Knowledge source changed while embedding.")
        c.execute(
            """INSERT INTO public.rag_document_summaries
          (library_id,generation,document_id,version,summary,search_text,draft,summary_sha256,draft_sha256,embedding_space_id)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(library_id,generation,document_id)
          DO UPDATE SET version=excluded.version,summary=excluded.summary,search_text=excluded.search_text,
          draft=excluded.draft,summary_sha256=excluded.summary_sha256,draft_sha256=excluded.draft_sha256,
          embedding_space_id=excluded.embedding_space_id,published_at=now()""",
            (
                library_id(video),
                generation,
                doc_id,
                version,
                summary,
                search,
                row[1],
                sha,
                row[2],
                space,
            ),
        )
        c.execute(
            "DELETE FROM public.rag_document_summary_chunks WHERE library_id=%s AND generation=%s AND document_id=%s",
            (library_id(video), generation, doc_id),
        )
        c.cursor().executemany(
            "INSERT INTO public.rag_document_summary_chunks VALUES(%s,%s,%s,%s,%s,%s)",
            [
                (library_id(video), generation, doc_id, n, chunk, Vector(list(v)))
                for n, (chunk, v) in enumerate(zip(chunks, vectors, strict=True))
            ],
        )
        c.execute(
            "UPDATE public.rag_document_summary_jobs SET state='complete',error_code=NULL,updated_at=now() "
            "WHERE library_id=%s AND generation=%s AND document_id=%s",
            (library_id(video), generation, doc_id),
        )
    atomic_text(knowledge_root(config) / str(doc_id) / "summary.md", summary)
    return {
        "state": "complete",
        "document_id": str(doc_id),
        "version": version,
        "chunks": len(chunks),
    }


def materialize_media_draft(config, video, job_id, draft):
    from .knowledge_migration import copy_image
    from .video_store import digest_file

    attachments = []
    root = (video.root / "derived" / str(job_id)).resolve()

    def image(match):
        label, reference = match.groups()
        if reference.startswith(("https:", "http:", "data:")):
            return match.group(0)
        source = (root / reference).resolve()
        if (
            not source.is_relative_to(root)
            or not source.is_file()
            or source.is_symlink()
        ):
            raise ConfigError("Unresolved retained media attachment: " + reference)
        record = {"sha256": digest_file(source), "suffix": source.suffix.lower()}
        copy_image(config, source, record)
        attachments.append(record)
        return f"![{label}](../attachments/{record['sha256']}{record['suffix']})"

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", image, draft), attachments


def sync_video_summary(config, video, job_id, *, generation=None):
    with connect_database(config) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        row = c.execute(
            "SELECT s.title,s.source_label,s.draft,s.summary,s.metadata,j.collection,j.payload "
            "FROM public.rag_knowledge_summaries s JOIN public.rag_video_jobs j ON j.id=s.job_id "
            "WHERE s.job_id=%s AND s.library_id=%s",
            (job_id, library_id(video)),
        ).fetchone()
    if not row:
        return
    meta = {
        **row[4],
        "legacy_collection": row[5],
        "source_job_id": str(job_id),
        "supersedes": row[6].get("supersedes", []),
    }
    draft, attachments = materialize_media_draft(config, video, job_id, row[2])
    meta["attachments"] = attachments
    meta["original_draft_sha256"] = text_hash(row[2])
    title = title_text(row[6].get("title")) or row[0]
    register_document(
        config,
        video,
        job_id,
        draft,
        title,
        row[1],
        meta,
        source_kind="media",
        source_job_id=job_id,
    )
    generation = generation or active_generation(config, video)
    queue_summary(config, video, generation, job_id)
    return publish_summary(config, video, generation, job_id, row[3])


def activate_generation(config, video, generation, expected_ids, *, allow_stale=False):
    lid = library_id(video)
    with connect_database(config) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("library:" + lid,))
        rows = c.execute(
            "SELECT document_id,draft_sha256,summary_sha256 FROM public.rag_document_summaries "
            "WHERE library_id=%s AND generation=%s",
            (lid, generation),
        ).fetchall()
        stale = c.execute(
            "SELECT count(*) FROM public.rag_document_summaries s JOIN public.rag_knowledge_documents d ON d.id=s.document_id WHERE s.library_id=%s AND s.generation=%s AND s.draft_sha256<>d.draft_sha256",
            (lid, generation),
        ).fetchone()[0]
        if stale and not allow_stale:
            raise ConfigError(
                "Generation contains changed sources; rebuild before activation."
            )
        present = {str(r[0]) for r in rows}
        if not set(expected_ids).issubset(present):
            raise ConfigError(
                f"Generation missing {len(set(expected_ids) - present)} required summaries."
            )
        digest = text_hash(json.dumps(sorted((str(r[0]), r[1], r[2]) for r in rows)))
        c.execute(
            "UPDATE public.rag_library_generations SET state='ready',manifest_sha256=%s WHERE library_id=%s AND generation=%s",
            (digest, lid, generation),
        )
        c.execute(
            "INSERT INTO public.rag_library_state VALUES(%s,%s) ON CONFLICT(library_id) DO UPDATE SET active_generation=excluded.active_generation",
            (lid, generation),
        )
        c.execute(
            "UPDATE public.rag_library_generations SET state='ready' WHERE library_id=%s AND state='active'",
            (lid,),
        )
        c.execute(
            "UPDATE public.rag_library_generations SET state='active' WHERE library_id=%s AND generation=%s",
            (lid, generation),
        )
    return {"generation": generation, "documents": len(rows), "manifest_sha256": digest}


def _title_peers(connection, video, identifiers, generation):
    rows = connection.execute(
        """SELECT selected.id,peer.id,peer.title,peer.source_label,peer.metadata
        FROM public.rag_knowledge_documents selected JOIN public.rag_knowledge_documents peer
        ON peer.library_id=selected.library_id AND peer.title_key=selected.title_key
        JOIN public.rag_document_summaries s ON s.document_id=peer.id AND s.generation=%s
        LEFT JOIN public.rag_video_jobs j ON j.id=peer.source_job_id
        LEFT JOIN public.rag_videos v ON v.id=j.id
        WHERE selected.library_id=%s AND selected.id=ANY(%s::uuid[])
        AND selected.title_key<>'' AND peer.id<>selected.id
        AND (j.id IS NULL OR (j.state='complete' AND (v.id IS NULL OR v.source_deleted)))
        ORDER BY selected.id,peer.id""",
        (generation, library_id(video), identifiers),
    ).fetchall()
    peers = {}
    for selected, ident, title, label, meta in rows:
        peers.setdefault(str(selected), []).append(
            {
                "document_id": ("video:" if meta.get("source_job_id") else "knowledge:")
                + str(ident),
                "title": title,
                "source_label": label,
                "source_urls": meta.get("source_urls", []),
            }
        )
    return peers


def search(config, video, query, limit):
    if not query.strip() or isinstance(limit, bool) or not 1 <= limit <= 5:
        raise ValueError("Invalid unified query or limit.")
    profile = resolve_index(config)
    lid = library_id(video)
    gen = active_generation(config, video)
    space = embedding_space_id(profile.embedding)
    vector = Vector(list(client_from_config(profile).embed_query(query.strip())))
    with connect_database(profile) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        rows = c.execute(
            """WITH candidates AS (SELECT d.id,d.title,d.source_label,d.metadata,s.summary,s.version,s.summary_sha256,
          s.draft_sha256,max(1-(ch.embedding <=> %s)) similarity,d.title_key
          FROM public.rag_document_summaries s JOIN public.rag_knowledge_documents d ON d.id=s.document_id
          JOIN public.rag_document_summary_chunks ch ON ch.library_id=s.library_id AND ch.generation=s.generation AND ch.document_id=s.document_id
          LEFT JOIN public.rag_video_jobs j ON j.id=d.source_job_id LEFT JOIN public.rag_videos v ON v.id=j.id
          WHERE s.library_id=%s AND s.generation=%s AND s.embedding_space_id=%s
          AND (j.id IS NULL OR (j.state='complete' AND (v.id IS NULL OR v.source_deleted)))
          AND NOT EXISTS(SELECT 1 FROM public.rag_knowledge_documents newer
            JOIN public.rag_document_summaries ns ON ns.document_id=newer.id AND ns.generation=s.generation
            JOIN public.rag_video_jobs nj ON nj.id=newer.source_job_id
            WHERE newer.library_id=s.library_id AND newer.metadata->'supersedes' ? d.id::text
            AND nj.state='complete')
          GROUP BY d.id,s.library_id,s.generation,s.document_id),
          unique_titles AS (SELECT *,row_number() OVER(PARTITION BY coalesce(nullif(title_key,''),id::text) ORDER BY similarity DESC,id) AS title_rank FROM candidates)
          SELECT id,title,source_label,metadata,summary,version,summary_sha256,draft_sha256,similarity FROM unique_titles
          WHERE title_rank=1 ORDER BY similarity DESC,id LIMIT %s""",
            (vector, lid, gen, space, limit),
        ).fetchall()
        peers = _title_peers(c, video, [r[0] for r in rows], gen)
    return [
        {
            "result_type": "document_summary",
            "document_id": ("video:" if r[3].get("source_job_id") else "knowledge:")
            + str(r[0]),
            "knowledge_base": "general",
            "title": r[1],
            "source_relative_path": str(r[0]) + "/summary.md",
            "section": "summary",
            "excerpt": r[4],
            "semantic_score": float(r[8]),
            "lexical_score": None,
            "combined_score": float(r[8]),
            "reliable": float(r[8]) >= 0.3,
            "content_hash": r[6],
            "metadata": {
                **r[3],
                "version": r[5],
                "draft_sha256": r[7],
                "source_label": r[2],
                "embedding_space_id": space,
                "read_tool": "document_read",
                "same_title_documents": peers.get(str(r[0]), []),
            },
        }
        for r in rows
    ]


def document_context(
    config, video, document_id, *, version=None, offset=0, max_characters=16000
):
    if not isinstance(document_id, str) or not re.fullmatch(
        r"(?:video|knowledge):[0-9a-f-]{36}", document_id
    ):
        raise ConfigError("Invalid document ID.")
    ident = str(UUID(document_id.split(":", 1)[1]))
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or isinstance(max_characters, bool)
        or not isinstance(max_characters, int)
        or not 1 <= max_characters <= 32000
    ):
        raise ValueError("Invalid document page.")
    if offset and not version:
        raise ValueError("Version required for later pages.")
    with connect_database(config) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        row = c.execute(
            """SELECT d.title,d.source_label,s.version,s.summary,s.draft,s.summary_sha256,s.draft_sha256,d.metadata
          FROM public.rag_document_summaries s JOIN public.rag_knowledge_documents d ON d.id=s.document_id
          LEFT JOIN public.rag_video_jobs j ON j.id=d.source_job_id LEFT JOIN public.rag_videos v ON v.id=j.id
          WHERE s.library_id=%s AND s.generation=%s AND d.id=%s
          AND (j.id IS NULL OR (j.state='complete' AND (v.id IS NULL OR v.source_deleted)))""",
            (library_id(video), active_generation(config, video), ident),
        ).fetchone()
        peers = (
            _title_peers(c, video, [ident], active_generation(config, video))
            if row
            else {}
        )
    if not row or (version is not None and version != row[2]):
        raise ConfigError("DOCUMENT_UNAVAILABLE_OR_VERSION_CHANGED")
    if text_hash(row[3]) != row[5] or text_hash(row[4]) != row[6]:
        raise ConfigError("Document integrity verification failed.")
    if offset > len(row[4]):
        raise ValueError("Offset beyond original.")
    end = min(len(row[4]), offset + max_characters)
    return {
        "ok": True,
        "document_id": document_id,
        "title": row[0],
        "source_label": row[1],
        "version": row[2],
        "summary": row[3],
        "original_document": row[4][offset:end],
        "summary_sha256": row[5],
        "draft_sha256": row[6],
        "metadata": {**row[7], "same_title_documents": peers.get(ident, [])},
        "offset": offset,
        "total_characters": len(row[4]),
        "next_offset": end if end < len(row[4]) else None,
        "complete": end == len(row[4]),
    }


def status(config, video):
    lid = library_id(video)
    gen = active_generation(config, video)
    with connect_database(config) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        rows = c.execute(
            "SELECT count(DISTINCT coalesce(nullif(d.title_key,''),d.id::text)),count(ch.ordinal),max(s.published_at) "
            "FROM public.rag_document_summaries s JOIN public.rag_document_summary_chunks ch USING(library_id,generation,document_id) "
            "LEFT JOIN public.rag_knowledge_documents d ON d.id=s.document_id LEFT JOIN public.rag_video_jobs j ON j.id=d.source_job_id "
            "LEFT JOIN public.rag_videos v ON v.id=j.id WHERE s.library_id=%s AND s.generation=%s "
            "AND (j.id IS NULL OR (j.state='complete' AND (v.id IS NULL OR v.source_deleted)))",
            (lid, gen),
        ).fetchone()
        states = c.execute(
            "SELECT state,count(*) FROM public.rag_document_summary_jobs WHERE library_id=%s AND generation=%s GROUP BY state",
            (lid, gen),
        ).fetchall()
        failures = c.execute(
            "SELECT document_id,error_code FROM public.rag_document_summary_jobs WHERE library_id=%s AND generation=%s AND state IN ('failed','unsearchable')",
            (lid, gen),
        ).fetchall()
    return {
        "retrieval_scope": "summaries_only",
        "default_top_k": 5,
        "published_documents": rows[0],
        "published_chunks": rows[1],
        "last_indexed_at": rows[2],
        "generation": gen,
        "job_states": dict(states),
        "failures": [{"document_id": str(r[0]), "error_code": r[1]} for r in failures],
        "coverage_complete": not failures
        and not any(s in {"queued", "running"} for s, n in states if n),
        "collections": {
            "general": {
                "documents": rows[0],
                "chunks": rows[1],
                "last_indexed_at": rows[2],
            }
        },
    }


def list_generations(config, video):
    with connect_database(config) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        rows = c.execute(
            "SELECT g.generation,g.state,g.manifest_sha256,coalesce(g.generation=s.active_generation,false) "
            "FROM public.rag_library_generations g LEFT JOIN public.rag_library_state s USING(library_id) "
            "WHERE g.library_id=%s ORDER BY g.created_at",
            (library_id(video),),
        ).fetchall()
    return [
        {"generation": r[0], "state": r[1], "manifest_sha256": r[2], "active": r[3]}
        for r in rows
    ]


def build_generation(config, video, generation):
    with connect_database(config) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        active = c.execute(
            "SELECT active_generation FROM public.rag_library_state WHERE library_id=%s",
            (library_id(video),),
        ).fetchone()
        if active and active[0] == generation:
            raise ConfigError(
                "Build a new shadow generation instead of the active one."
            )
        rows = c.execute(
            "SELECT id,title,draft,source_job_id FROM public.rag_knowledge_documents WHERE library_id=%s",
            (library_id(video),),
        ).fetchall()
    create_generation(config, video, generation)
    done = []
    for ident, title, draft, job in rows:
        queue_summary(config, video, generation, ident)
        if job:
            sync_video_summary(config, video, str(job), generation=generation)
        else:
            publish_summary(
                config, video, generation, ident, extractive_summary(draft, title)
            )
        done.append(str(ident))
    return {"generation": generation, "documents": len(done), "state": "built"}


def activate_built_generation(config, video, generation, *, allow_stale=False):
    with connect_database(config) as c:
        from .pipeline_fence import assert_owner

        assert_owner(c)
        if allow_stale:
            ids = c.execute(
                "SELECT document_id FROM public.rag_document_summaries WHERE library_id=%s AND generation=%s",
                (library_id(video), generation),
            ).fetchall()
        else:
            ids = c.execute(
                "SELECT d.id FROM public.rag_knowledge_documents d LEFT JOIN public.rag_video_jobs j ON j.id=d.source_job_id WHERE d.library_id=%s AND (j.id IS NULL OR j.state='complete') AND NOT EXISTS(SELECT 1 FROM public.rag_document_summary_jobs q WHERE q.document_id=d.id AND q.state='unsearchable')",
                (library_id(video),),
            ).fetchall()
    if not ids:
        raise ConfigError("Cannot activate an empty generation.")
    return activate_generation(config, video, generation, [str(r[0]) for r in ids])
