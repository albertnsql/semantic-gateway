import os
import random
import uuid
from datetime import date, datetime, timedelta
from collections import defaultdict

import numpy as np
import pandas as pd

random.seed(42)
np.random.seed(42)

OUTPUT_DIR = "output"

SIM_END = date(2026, 5, 31)

# ── HELPERS ─────────────────────────────────────────────

def uid():
    return str(uuid.uuid4())

def rand_date(start, end):
    if start > end:
        return end
    return start + timedelta(days=random.randint(0, (end - start).days))

# ── LOAD EXISTING CSVs ─────────────────────────────────

print("Loading existing datasets...")

content_df = pd.read_csv(f"{OUTPUT_DIR}/content_catalog.csv")
sub_df = pd.read_csv(f"{OUTPUT_DIR}/subscribers.csv")
sessions_df = pd.read_csv(f"{OUTPUT_DIR}/stream_sessions.csv")

print("Building lookup maps...")

# content ids
content_ids = content_df["content_id"].tolist()

# fake popularity weights (uniform now, good enough)
content_weights = [1] * len(content_ids)

# subscriber info lookup
sub_info = {
    row["subscriber_id"]: row.to_dict()
    for _, row in sub_df.iterrows()
}

# session lookup by subscriber
session_ids_by_sub = defaultdict(list)

for _, row in sessions_df.iterrows():
    session_ids_by_sub[row["subscriber_id"]].append(row["session_id"])

# ── GENERATE WATCHLISTS ────────────────────────────────

print("Generating user_watchlists (~50k rows)...")

watchlist_rows = []
TARGET_WL = 50_000

sources = ["browse", "recommendation", "search", "share", "trailer"]

subscriber_ids = list(sub_info.keys())

for i in range(TARGET_WL):

    if i % 5000 == 0:
        print(f"  Progress: {i:,}/{TARGET_WL:,}")

    sid = random.choice(subscriber_ids)

    info = sub_info[sid]

    signup = date.fromisoformat(info["signup_date"])

    churn_d = (
        date.fromisoformat(info["churn_date"])
        if pd.notna(info["churn_date"])
        else SIM_END
    )

    active_end = min(churn_d, SIM_END)

    add_d = rand_date(signup, active_end)

    cid = random.choices(
        content_ids,
        weights=content_weights,
        k=1
    )[0]

    # removal logic
    removed = random.random() < 0.35
    remove_ts = None

    if removed:

        remove_start = add_d + timedelta(days=1)
        remove_end = active_end

        # FIXED BUG
        if remove_start <= remove_end:

            remove_d = rand_date(remove_start, remove_end)

            remove_ts = datetime(
                remove_d.year,
                remove_d.month,
                remove_d.day
            ).isoformat()

    # streamed logic
    was_streamed = random.random() < 0.42

    linked_sess = None

    if was_streamed:

        subs_sessions = session_ids_by_sub.get(sid, [])

        if subs_sessions:
            linked_sess = random.choice(subs_sessions)

    watchlist_rows.append({
        "watchlist_id": uid(),
        "subscriber_id": sid,
        "content_id": cid,
        "added_timestamp": datetime(
            add_d.year,
            add_d.month,
            add_d.day,
            random.randint(0, 23),
            random.randint(0, 59)
        ).isoformat(),
        "removed_timestamp": remove_ts,
        "was_streamed": was_streamed,
        "stream_session_id": linked_sess,
        "source": random.choices(
            sources,
            weights=[0.30, 0.28, 0.20, 0.12, 0.10],
            k=1
        )[0],
    })

# ── EXPORT ─────────────────────────────────────────────

watchlist_df = pd.DataFrame(watchlist_rows)

output_path = f"{OUTPUT_DIR}/user_watchlists.csv"

watchlist_df.to_csv(output_path, index=False)

print(f"\nuser_watchlists: {len(watchlist_df):,} rows")
print(f"Saved to: {output_path}")

print("\nDONE")