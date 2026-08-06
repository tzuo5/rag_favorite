BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS public.cooking_recipes (
    id bigserial PRIMARY KEY,
    source_path text NOT NULL UNIQUE,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 300),
    cuisine text,
    category text,
    document_type text NOT NULL CHECK (document_type IN ('recipe', 'menu_plan')),
    content text NOT NULL,
    image_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    index_version integer NOT NULL DEFAULT 3,
    embedding_model text NOT NULL DEFAULT 'qwen3-embedding:0.6b',
    indexed_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.cooking_recipes
    ADD COLUMN IF NOT EXISTS index_version integer NOT NULL DEFAULT 2;
ALTER TABLE public.cooking_recipes ALTER COLUMN index_version SET DEFAULT 3;
ALTER TABLE public.cooking_recipes
    ADD COLUMN IF NOT EXISTS embedding_model text NOT NULL
        DEFAULT 'qwen3-embedding:0.6b';

CREATE TABLE IF NOT EXISTS public.cooking_recipe_sections (
    id bigserial PRIMARY KEY,
    recipe_id bigint NOT NULL REFERENCES public.cooking_recipes(id) ON DELETE CASCADE,
    section_index integer NOT NULL CHECK (section_index >= 0),
    heading_path text NOT NULL,
    content text NOT NULL,
    embedding vector(1024) NOT NULL,
    UNIQUE (recipe_id, section_index)
);

CREATE TABLE IF NOT EXISTS public.cooking_recipe_events (
    id bigserial PRIMARY KEY,
    request_hash text NOT NULL UNIQUE CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    operation text NOT NULL CHECK (operation = 'create'),
    source_path text NOT NULL,
    title text NOT NULL,
    recipe_id bigint NOT NULL REFERENCES public.cooking_recipes(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cooking_recipe_title_trgm_idx
    ON public.cooking_recipes USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS cooking_recipe_content_trgm_idx
    ON public.cooking_recipes USING gin (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS cooking_section_content_trgm_idx
    ON public.cooking_recipe_sections USING gin (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS cooking_section_recipe_idx
    ON public.cooking_recipe_sections (recipe_id);

CREATE OR REPLACE FUNCTION public.cooking_api_search(
    query_text text,
    query_embedding vector(1024),
    result_limit integer DEFAULT 5
)
RETURNS TABLE (
    recipe_id bigint,
    title text,
    cuisine text,
    category text,
    document_type text,
    heading_path text,
    excerpt text,
    source_path text,
    cosine_similarity double precision,
    lexical_similarity double precision,
    hybrid_score double precision,
    reliable boolean
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
WITH scored AS (
    SELECT
        s.id AS section_id,
        r.id AS recipe_id,
        r.title,
        r.cuisine,
        r.category,
        r.document_type,
        s.heading_path,
        s.content,
        r.source_path,
        (1 - (s.embedding <=> query_embedding))::double precision AS cosine_score,
        greatest(
            similarity(lower(r.title), lower(query_text)),
            similarity(lower(s.heading_path), lower(query_text)),
            similarity(lower(s.content), lower(query_text)),
            CASE
                WHEN strpos(lower(r.title), lower(query_text)) > 0 THEN 1.0
                WHEN strpos(lower(s.content), lower(query_text)) > 0 THEN 0.9
                ELSE 0.0
            END
        )::double precision AS lexical_score
    FROM public.cooking_recipe_sections AS s
    JOIN public.cooking_recipes AS r ON r.id = s.recipe_id
),
vector_ranked AS (
    SELECT section_id, row_number() OVER (ORDER BY cosine_score DESC) AS rank
    FROM scored
    ORDER BY cosine_score DESC
    LIMIT 30
),
lexical_ranked AS (
    SELECT section_id, row_number() OVER (ORDER BY lexical_score DESC) AS rank
    FROM scored
    WHERE lexical_score >= 0.12
    ORDER BY lexical_score DESC
    LIMIT 30
),
candidates AS (
    SELECT section_id FROM vector_ranked
    UNION
    SELECT section_id FROM lexical_ranked
)
SELECT
    x.recipe_id,
    x.title,
    x.cuisine,
    x.category,
    x.document_type,
    x.heading_path,
    left(x.content, 1800) AS excerpt,
    x.source_path,
    round(x.cosine_score::numeric, 6)::double precision,
    round(x.lexical_score::numeric, 6)::double precision,
    round((
        coalesce(1.0 / (60 + vr.rank), 0.0)
        + coalesce(1.0 / (60 + lr.rank), 0.0)
    )::numeric, 8)::double precision AS hybrid_score,
    (x.cosine_score >= 0.55 OR x.lexical_score >= 0.18) AS reliable
FROM candidates AS c
JOIN scored AS x ON x.section_id = c.section_id
LEFT JOIN vector_ranked AS vr ON vr.section_id = c.section_id
LEFT JOIN lexical_ranked AS lr ON lr.section_id = c.section_id
ORDER BY hybrid_score DESC, x.cosine_score DESC
LIMIT greatest(1, least(result_limit, 10));
$$;

CREATE OR REPLACE FUNCTION public.cooking_api_get(requested_recipe_id bigint)
RETURNS TABLE (
    recipe_id bigint,
    title text,
    cuisine text,
    category text,
    document_type text,
    content text,
    content_truncated boolean,
    source_path text,
    image_refs jsonb,
    indexed_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
SELECT
    r.id,
    r.title,
    r.cuisine,
    r.category,
    r.document_type,
    left(r.content, 16000),
    char_length(r.content) > 16000,
    r.source_path,
    r.image_refs,
    r.indexed_at
FROM public.cooking_recipes AS r
WHERE r.id = requested_recipe_id;
$$;

CREATE OR REPLACE FUNCTION public.cooking_api_status()
RETURNS TABLE (
    recipes bigint,
    sections bigint,
    last_indexed_at timestamptz,
    embedding_model text,
    embedding_dimensions integer
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
SELECT
    (SELECT count(*) FROM public.cooking_recipes),
    (SELECT count(*) FROM public.cooking_recipe_sections),
    (SELECT max(indexed_at) FROM public.cooking_recipes),
    'qwen3-embedding:0.6b'::text,
    1024;
$$;

REVOKE ALL ON public.cooking_recipes FROM PUBLIC;
REVOKE ALL ON public.cooking_recipe_sections FROM PUBLIC;
REVOKE ALL ON public.cooking_recipe_events FROM PUBLIC;
REVOKE ALL ON FUNCTION public.cooking_api_search(text, vector, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.cooking_api_get(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.cooking_api_status() FROM PUBLIC;

COMMIT;
