"""Graph seed helpers for service-level tests.

Builds a small consistent fixture: 4 users (alice, bob, carol, newbie),
5 authors, 4 series, 6 categories, 13 books, plus reading history that
exercises every signal.

Intentionally NOT named ``test_*.py`` so pytest doesn't try to collect it.
"""
from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List


def _ts(days_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def seed_graph(conn) -> Dict[str, str]:
    """Populate a fresh KuzuDB connection with deterministic test data.

    Returns a dict mapping semantic names (user_alice, book_dune, ...) to
    UUIDs so tests can reference seeded entities without hard-coding ids.
    """
    ids: Dict[str, str] = {}

    def new_id(name: str) -> str:
        ids[name] = str(uuid.uuid4())
        return ids[name]

    # Lazily create HAS_PERSONAL_METADATA — production app does this on
    # first write; we do it up-front so the seed can write to the rel.
    try:
        conn.execute(
            "CREATE REL TABLE HAS_PERSONAL_METADATA(FROM User TO Book, "
            "personal_notes STRING, start_date TIMESTAMP, finish_date TIMESTAMP, "
            "personal_custom_fields STRING, created_at TIMESTAMP, updated_at TIMESTAMP)",
            {},
        )
    except Exception:
        pass  # Already exists from a prior session

    for handle in ("alice", "bob", "carol", "newbie"):
        uid = new_id(f"user_{handle}")
        conn.execute(
            "CREATE (:User {id: $id, username: $u, email: $e, "
            "share_library: false, share_current_reading: true, "
            "share_reading_activity: true, is_admin: false, "
            "created_at: $ts, updated_at: $ts})",
            {"id": uid, "u": handle, "e": f"{handle}@example.com", "ts": _ts(0)},
        )

    author_specs = [
        ("Frank Herbert", "herbert"),
        ("Isaac Asimov", "asimov"),
        ("Ursula K. Le Guin", "le_guin"),
        ("Brandon Sanderson", "sanderson"),
        ("Andy Weir", "weir"),
    ]
    for name, slug in author_specs:
        pid = new_id(f"author_{slug}")
        conn.execute(
            "CREATE (:Person {id: $id, name: $n, normalized_name: $nn})",
            {"id": pid, "n": name, "nn": name.lower()},
        )

    for sname in ("Dune", "Foundation", "Earthsea", "Stormlight Archive"):
        sid = new_id(f"series_{sname.lower().replace(' ', '_')}")
        conn.execute(
            "CREATE (:Series {id: $id, name: $n, normalized_name: $nn})",
            {"id": sid, "n": sname, "nn": sname.lower()},
        )

    # Map short alias -> (display name, normalized name) so book_specs can
    # use short keys (e.g. "sci_fi") while we still store canonical names.
    category_specs = [
        ("sci_fi",             "Science Fiction",    "science fiction"),
        ("fantasy",            "Fantasy",            "fantasy"),
        ("space_opera",        "Space Opera",        "space opera"),
        ("hard_science_fiction","Hard Science Fiction","hard science fiction"),
        ("epic_fantasy",       "Epic Fantasy",       "epic fantasy"),
        ("classic",            "Classic",            "classic"),
    ]
    for alias, display_name, norm_name in category_specs:
        cid = new_id(f"cat_{alias}")
        conn.execute(
            "CREATE (:Category {id: $id, name: $n, normalized_name: $nn})",
            {"id": cid, "n": display_name, "nn": norm_name},
        )

    book_specs: List = [
        ("dune", "Dune", "en", "herbert", "dune", 1, ["sci_fi", "space_opera"]),
        ("dune2", "Dune Messiah", "en", "herbert", "dune", 2, ["sci_fi", "space_opera"]),
        ("dune3", "Children of Dune", "en", "herbert", "dune", 3, ["sci_fi"]),
        ("foundation", "Foundation", "en", "asimov", "foundation", 1, ["sci_fi", "classic"]),
        ("foundation2", "Foundation and Empire", "en", "asimov", "foundation", 2, ["sci_fi", "classic"]),
        ("foundation3", "Second Foundation", "en", "asimov", "foundation", 3, ["sci_fi", "classic"]),
        ("earthsea", "A Wizard of Earthsea", "en", "le_guin", "earthsea", 1, ["fantasy"]),
        ("earthsea2", "The Tombs of Atuan", "en", "le_guin", "earthsea", 2, ["fantasy"]),
        ("storm1", "The Way of Kings", "en", "sanderson", "stormlight_archive", 1, ["fantasy", "epic_fantasy"]),
        ("storm2", "Words of Radiance", "en", "sanderson", "stormlight_archive", 2, ["fantasy", "epic_fantasy"]),
        ("hail_mary", "Project Hail Mary", "en", "weir", None, None, ["sci_fi", "hard_science_fiction"]),
        ("martian", "The Martian", "en", "weir", None, None, ["sci_fi", "hard_science_fiction"]),
        ("dispossessed", "The Dispossessed", "en", "le_guin", None, None, ["sci_fi"]),
    ]
    for slug, title, lang, author_slug, series_slug, vol, cats in book_specs:
        bid = new_id(f"book_{slug}")
        conn.execute(
            "CREATE (:Book {id: $id, title: $t, normalized_title: $nt, "
            "language: $l, created_at: $ts, updated_at: $ts})",
            {"id": bid, "t": title, "nt": title.lower(), "l": lang, "ts": _ts(0)},
        )
        conn.execute(
            "MATCH (p:Person {id: $pid}), (b:Book {id: $bid}) "
            "CREATE (p)-[:AUTHORED {role: 'authored', order_index: 0}]->(b)",
            {"pid": ids[f"author_{author_slug}"], "bid": bid},
        )
        if series_slug:
            conn.execute(
                "MATCH (b:Book {id: $bid}), (s:Series {id: $sid}) "
                "CREATE (b)-[:PART_OF_SERIES {volume_number: $vol}]->(s)",
                {"bid": bid, "sid": ids[f"series_{series_slug}"], "vol": vol},
            )
        for cat in cats:
            conn.execute(
                "MATCH (b:Book {id: $bid}), (c:Category {id: $cid}) "
                "CREATE (b)-[:CATEGORIZED_AS {created_at: $ts}]->(c)",
                {"bid": bid, "cid": ids[f"cat_{cat}"], "ts": _ts(0)},
            )

    # Reading history — alice/bob/carol have finishes; newbie does not.
    finish_plan = {
        "alice": [("dune", 90), ("dune2", 60), ("dune3", 30), ("foundation", 20)],
        "bob":   [("dune", 95), ("dune2", 70), ("foundation", 40), ("foundation2", 25),
                  ("storm1", 200), ("storm2", 100)],
        "carol": [("dune", 80), ("foundation", 50), ("hail_mary", 15), ("martian", 5)],
    }
    for user_handle, finishes in finish_plan.items():
        for slug, days_ago in finishes:
            conn.execute(
                "MATCH (u:User {id: $uid}), (b:Book {id: $bid}) "
                "CREATE (u)-[:HAS_PERSONAL_METADATA {"
                "finish_date: $fd, created_at: $ts, updated_at: $ts}]->(b)",
                {
                    "uid": ids[f"user_{user_handle}"],
                    "bid": ids[f"book_{slug}"],
                    "fd": _ts(days_ago),
                    "ts": _ts(days_ago),
                },
            )

    return ids
