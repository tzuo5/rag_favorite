BEGIN;

GRANT SELECT, INSERT, UPDATE, DELETE ON public.cooking_recipes TO cooking_rag_ingest;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.cooking_recipe_sections TO cooking_rag_ingest;
GRANT USAGE, SELECT ON SEQUENCE public.cooking_recipes_id_seq TO cooking_rag_ingest;
GRANT USAGE, SELECT ON SEQUENCE public.cooking_recipe_sections_id_seq TO cooking_rag_ingest;
GRANT SELECT, INSERT ON public.cooking_recipe_events TO cooking_rag_ingest;
GRANT USAGE, SELECT ON SEQUENCE public.cooking_recipe_events_id_seq TO cooking_rag_ingest;

GRANT SELECT, INSERT, UPDATE, DELETE ON public.cooking_recipes TO cooking_rag_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.cooking_recipe_sections TO cooking_rag_writer;
GRANT SELECT, INSERT ON public.cooking_recipe_events TO cooking_rag_writer;
GRANT USAGE, SELECT ON SEQUENCE public.cooking_recipes_id_seq TO cooking_rag_writer;
GRANT USAGE, SELECT ON SEQUENCE public.cooking_recipe_sections_id_seq TO cooking_rag_writer;
GRANT USAGE, SELECT ON SEQUENCE public.cooking_recipe_events_id_seq TO cooking_rag_writer;

REVOKE ALL ON public.cooking_recipes FROM cooking_rag_runtime;
REVOKE ALL ON public.cooking_recipe_sections FROM cooking_rag_runtime;
REVOKE ALL ON public.cooking_recipe_events FROM cooking_rag_runtime;
GRANT EXECUTE ON FUNCTION public.cooking_api_search(text, vector, integer)
    TO cooking_rag_runtime;
GRANT EXECUTE ON FUNCTION public.cooking_api_get(bigint)
    TO cooking_rag_runtime;
GRANT EXECUTE ON FUNCTION public.cooking_api_status()
    TO cooking_rag_runtime;

COMMIT;
