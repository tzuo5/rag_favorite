# Fourteen-day observation

Observation starts after the owner Telegram canary passes. The earliest
retirement decision is 14 full days after that timestamp.

Run the metadata-only report without exposing queries or retrieved content:

```bash
/home/ubuntu/services/rag-mcp/.venv/bin/python \
  /home/ubuntu/services/rag-mcp/observe_eight_category.py \
  --after 2026-07-28T00:00:00Z
```

Review:

- unified Cooking search call count;
- legacy Cooking search/status call count;
- unified no-reliable-match rate;
- unified error rate;
- full recipe get count and failure rate;
- image delivery failure count;
- explicit cross-library rate;
- recipe create call count.

Wrong-category rate requires a manual review of a bounded random sample because
tool metadata alone cannot determine user intent. Do not export queries,
excerpts, private source text, credentials, or image paths for this review.

Old search/status retirement is allowed only after the observation window has
elapsed and the owner accepts the measured error, no-match, wrong-category,
full-get, and image-delivery rates.
