"""Candidate retrieval: ask Elasticsearch for watchlist names that look like
the applicant's name. This is our stand-in for today's 'flagged by name'."""
from app.clients import ES_INDEX, get_es

CANDIDATE_LIMIT = 50    # keep the top 50 look-alikes per applicant


def build_query(first_name: str, middle_name: str | None, last_name: str) -> dict:
    full_name = " ".join(p for p in (first_name, middle_name, last_name) if p)
    return {"bool": {
        # MUST: the surname matches, allowing typos (fuzziness AUTO = 1-2 edits)
        "must": [{"match": {"last_name": {"query": last_name, "fuzziness": "AUTO"}}}],
        # SHOULD: optional, but candidates matching these rank higher
        "should": [
            {"match": {"first_name": {"query": first_name, "fuzziness": "AUTO"}}},
            {"match": {"full_name": {"query": full_name, "fuzziness": "AUTO"}}},
        ],
    }}


def search_candidates(first_name: str, middle_name: str | None, last_name: str,
                      size: int = CANDIDATE_LIMIT) -> list[dict]:
    resp = get_es().search(index=ES_INDEX, size=size,
                           query=build_query(first_name, middle_name, last_name))
    return [{"pg_id": h["_source"]["pg_id"],
             "record_no": h["_source"]["record_no"],
             "es_score": float(h["_score"])} for h in resp["hits"]["hits"]]