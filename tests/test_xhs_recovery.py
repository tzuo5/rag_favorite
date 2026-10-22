from rag_favorite.xhs_recovery import collect_candidates, recover_payload


def test_recovery_uses_exact_identity_across_aliases_and_preserves_checkpoints():
    url = "https://www.xiaohongshu.com/explore/" + "a" * 24
    candidates = {}
    collect_candidates(
        {
            "previous_payload": {
                "source_url": url + "?xsec_token=private",
                "source_author_id": "author",
                "source_page_cursor": "cursor",
            }
        },
        candidates,
    )
    payload = {"source_url": url, "supersedes": ["old"], "asset_id": "keep"}
    recovered, category, changed = recover_payload(payload, candidates)
    assert changed and category == "awaiting_source_validation"
    assert recovered["source_url"].endswith("xsec_token=private")
    assert recovered["source_author_id"] == "author"
    assert recovered["supersedes"] == ["old"] and recovered["asset_id"] == "keep"
    assert "?" not in payload["source_url"]
    other = dict(payload, source_url=url.replace("a" * 24, "b" * 24))
    assert recover_payload(other, candidates)[1:] == ("needs_source_url", False)


def test_metadata_source_urls_and_newest_candidate_are_retained():
    url = "https://www.xiaohongshu.com/explore/" + "a" * 24
    candidates = {}
    collect_candidates({"source_urls": [url + "?xsec_token=new"]}, candidates)
    collect_candidates({"source_urls": [url + "?xsec_token=old"]}, candidates)
    recovered, _, _ = recover_payload({"source_url": url}, candidates)
    assert recovered["source_url"].endswith("xsec_token=new")
