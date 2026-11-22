"""cortex CLI: init / capture / query / decay / snapshot / restore / forget."""

import argparse
import json
import sys

from . import capture, db, decay, retrieve, snapshot


def cmd_init(args):
    db.init_db()
    print(f"initialized {db.db_path()}")


def _resolve_namespace(conn, name):
    if not name:
        return db.ensure_default_namespace(conn)
    return db.get_or_create_namespace(conn, name)


def cmd_capture(args):
    conn = db.connect()
    try:
        ns = _resolve_namespace(conn, args.namespace)
        result = capture.ingest(conn, ns, args.event_type, args.content, source_type=args.source)
        print(json.dumps(result, indent=2))
    finally:
        conn.close()


def cmd_query(args):
    conn = db.connect()
    try:
        ns = _resolve_namespace(conn, args.namespace)
        result = retrieve.query(conn, ns, args.text, limit=args.limit)
        print(result["text"])
    finally:
        conn.close()


def cmd_decay(args):
    conn = db.connect()
    try:
        ns = _resolve_namespace(conn, args.namespace) if args.namespace else None
        n = decay.recalc_importance(conn, ns)
        print(f"recalculated importance for {n} facts")
    finally:
        conn.close()


def cmd_snapshot_create(args):
    print(json.dumps(snapshot.create(reason=args.reason), indent=2))


def cmd_snapshot_list(args):
    print(json.dumps(snapshot.list_snapshots(), indent=2))


def cmd_snapshot_restore(args):
    print(json.dumps(snapshot.restore(args.snapshot_id), indent=2))


def cmd_forget(args):
    print(json.dumps(snapshot.forget(args.namespace or "global:user", subject=args.subject, fact_id=args.fact_id), indent=2))


def main():
    p = argparse.ArgumentParser(prog="cortex")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    pc = sub.add_parser("capture")
    pc.add_argument("event_type")
    pc.add_argument("content")
    pc.add_argument("--namespace", default=None)
    pc.add_argument("--source", default="agent_observed")
    pc.set_defaults(func=cmd_capture)

    pq = sub.add_parser("query")
    pq.add_argument("text")
    pq.add_argument("--namespace", default=None)
    pq.add_argument("--limit", type=int, default=10)
    pq.set_defaults(func=cmd_query)

    pd = sub.add_parser("decay")
    pd.add_argument("--namespace", default=None)
    pd.set_defaults(func=cmd_decay)

    ps = sub.add_parser("snapshot")
    ssub = ps.add_subparsers(dest="snap_command", required=True)
    sc = ssub.add_parser("create"); sc.add_argument("--reason", default="manual"); sc.set_defaults(func=cmd_snapshot_create)
    sl = ssub.add_parser("list"); sl.set_defaults(func=cmd_snapshot_list)
    sr = ssub.add_parser("restore"); sr.add_argument("snapshot_id"); sr.set_defaults(func=cmd_snapshot_restore)

    pf = sub.add_parser("forget")
    pf.add_argument("--namespace", default=None)
    pf.add_argument("--subject", default=None)
    pf.add_argument("--fact-id", dest="fact_id", default=None)
    pf.set_defaults(func=cmd_forget)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
