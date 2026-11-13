"""Run the pinned Threads GraphQL reader in a credential-free process."""
import json
from pathlib import Path
import sys


def read(root, owner, code):
    sys.path.insert(0, root)
    import config
    from threads import posts, refs, users
    from threads.fetch import ThreadsBadRequest, ThreadsBlocked, ThreadsNotFound, ThreadsUpstreamError
    config.MAX_RETRIES = 1
    try:
        if code:
            records = [posts.get_details(refs.resolve_post(code))]
        else:
            page = users.get_threads({"username": owner})
            records = [p for thread in page["threads"] for p in thread["posts"]]
    except ThreadsBlocked as exc:
        return [], next(("http_" + s for s in ("401", "403", "429") if "HTTP " + s in str(exc)), "challenge_required")
    except ThreadsBadRequest:
        return [], "request_rejected"
    except ThreadsNotFound:
        return [], "unavailable"
    except ThreadsUpstreamError:
        return [], "upstream_error"
    rows = []
    for record in records:
        media = record.get("media") or {}
        links = [part.get(k) for part in [media, *(media.get("items") or [])] for k in ("image", "video")]
        rows.append({"permalink": record["link"], "username": record["author"]["username"],
                     "text": record["text"], "timestamp": record["published_at"],
                     "media_urls": [link for link in links if isinstance(link, str)], **(record.get("stats") or {})})
    return rows, "unverified_posts" if rows else "no_public_posts"


if __name__ == "__main__":
    rows, status = read(*sys.argv[1:4])
    Path(sys.argv[4]).write_text(json.dumps({"rows": rows, "status": status}))
