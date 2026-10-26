import json
import re
from pathlib import Path


STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "into", "is", "it", "of", "on", "or", "our", "that", "the", "their",
    "to", "with", "we", "will", "you",
}


def _tokens(text):
    tokens = set()
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        if token.endswith("ing") and len(token) > 5:
            token = token[:-3]
        if len(token) > 2 and token not in STOP_WORDS:
            tokens.add(token)
    return tokens


def _load_json(path):
    with Path(path).open(encoding="utf-8") as file:
        return json.load(file)


def retrieve_context(query, data_dir, limit=4):
    """Return the best matching templates and sample briefs using token overlap."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return []

    data_dir = Path(data_dir)
    sources = (
        ("template", _load_json(data_dir / "templates.json")),
        ("example", _load_json(data_dir / "briefs.json")),
    )
    matches = []
    for source_type, documents in sources:
        for document in documents:
            searchable = " ".join(
                str(value)
                for key, value in document.items()
                if key not in {"id", "company"}
            )
            document_tokens = _tokens(searchable)
            overlap = query_tokens.intersection(document_tokens)
            if overlap:
                score = len(overlap) / len(query_tokens)
                matches.append(
                    {
                        "id": document["id"],
                        "type": source_type,
                        "title": document.get("title", document.get("company", "")),
                        "reason": "Matched: " + ", ".join(sorted(overlap)[:6]),
                        "score": score,
                        "content": document,
                    }
                )
    matches.sort(key=lambda item: (-item["score"], item["type"], item["id"]))
    return matches[:limit]
