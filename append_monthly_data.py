"""
append_monthly_data.py -- Monthly incremental data loader
=========================================================
Run this script once after each calendar month ends to:
  1. Generate synthetic event data for that month and append it to the
     existing CSVs in output/.
  2. Upload the new rows to Snowflake and INSERT them (no truncate).

Stateful tables (subscribers, subscriptions, subscription_plan_history)
are MERGED -- new subscriber IDs are inserted, existing ones are left alone.

Event tables (payments, stream_sessions, recommendation_events,
search_events, user_watchlists) are append-only: rows are inserted
without touching existing data.

Usage
-----
    # Append June 2026 (run after June 30 has passed)
    python append_monthly_data.py --month 2026-06

    # Append multiple months at once
    python append_monthly_data.py --month 2026-06 --month 2026-07

    # Dry run -- generate CSVs only, skip Snowflake upload
    python append_monthly_data.py --month 2026-06 --dry-run

    # Preview row counts without writing anything
    python append_monthly_data.py --month 2026-06 --preview

Requires
--------
    pip install faker numpy pandas snowflake-connector-python
"""

from __future__ import annotations

import argparse
import math
import os
import random
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from faker import Faker

# ── Snowflake credentials (read from gateway/.env) ────────────────────────────
_ENV_PATH = os.path.join(os.path.dirname(__file__), "gateway", ".env")
_env: dict[str, str] = {}
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, "r") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _env[_k.strip()] = _v.strip()

SF_USER      = _env.get("SNOWFLAKE_USER",     "ALBERT")
SF_PASSWORD  = _env.get("SNOWFLAKE_PASSWORD", "")
SF_ACCOUNT   = _env.get("SNOWFLAKE_ACCOUNT",  "")
SF_DATABASE  = _env.get("SNOWFLAKE_DATABASE", "STREAMING_ANALYTICS")
SF_WAREHOUSE = _env.get("SNOWFLAKE_WAREHOUSE","COMPUTE_WH")
SF_ROLE      = _env.get("SNOWFLAKE_ROLE",     "ACCOUNTADMIN")
SF_SCHEMA    = "RAW"

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")

# ── Fixed seeds -- must match generate_streaming_data.py so subscriber IDs
#    and content IDs are identical across all runs (foreign key safety). ────────
fake = Faker()
Faker.seed(42)
random.seed(42)
np.random.seed(42)

# ── Constants (copied verbatim from the base generator) ───────────────────────
COUNTRIES = {
    "US": 0.38, "IN": 0.12, "GB": 0.08, "DE": 0.06, "BR": 0.06,
    "CA": 0.05, "FR": 0.05, "AU": 0.04, "MX": 0.04, "JP": 0.03,
    "KR": 0.03, "NG": 0.02, "ZA": 0.02, "AR": 0.01, "SG": 0.01,
}
ACQ_CHANNELS = {
    "organic_search": 0.28, "paid_search": 0.22, "social": 0.18,
    "referral": 0.15, "email": 0.10, "tv_ad": 0.07,
}
PLANS = {
    "basic":    {"price": 8.99,  "weight": 0.25},
    "standard": {"price": 15.49, "weight": 0.45},
    "premium":  {"price": 22.99, "weight": 0.30},
}
PLAN_NAMES   = list(PLANS.keys())
PLAN_WEIGHTS = [PLANS[p]["weight"] for p in PLAN_NAMES]
PLAN_PRICES  = {p: PLANS[p]["price"] for p in PLAN_NAMES}
PLAN_ORDER   = {"basic": 0, "standard": 1, "premium": 2}
CHURN_REASONS   = ["price", "content_library", "competitor", "involuntary_payment",
                   "technical_issues", "taking_a_break"]
CONTENT_TYPES   = {"movie": 0.45, "series": 0.35, "documentary": 0.12, "short": 0.08}
MATURITY_RATINGS= ["G", "PG", "PG-13", "R", "TV-MA"]
LANGUAGES       = ["en", "es", "fr", "de", "ja", "ko", "pt", "hi", "ar"]
DEVICES         = ["tv", "mobile", "desktop", "tablet"]
QUALITY_LEVELS  = ["SD", "HD", "4K"]
REC_TYPES       = ["because_you_watched", "trending", "new_release",
                   "top_picks", "continue_watching"]
PAYMENT_METHODS = ["card", "paypal", "appstore", "googleplay"]
FAILURE_REASONS = ["insufficient_funds", "card_expired", "fraud_detected",
                   "bank_declined", "invalid_card"]
PRIMARY_GENRES  = ["drama", "thriller", "comedy", "action", "sci-fi",
                   "horror", "romance", "documentary", "animation", "crime"]
SECONDARY_TAGS  = ["dark", "feel-good", "based-on-true-story", "award-winning",
                   "binge-worthy", "family", "lgbtq+", "international", "cult-classic",
                   "mind-bending", "violent", "slow-burn", "witty", "inspirational"]
MOODS           = ["intense", "light-hearted", "thought-provoking", "suspenseful", "emotional"]
DIRECTORS       = [fake.name() for _ in range(300)]

currency_map = {
    "US": ("USD", 1.0),    "IN": ("INR", 83.0),  "GB": ("GBP", 0.79),
    "DE": ("EUR", 0.92),   "BR": ("BRL", 5.0),   "CA": ("CAD", 1.36),
    "FR": ("EUR", 0.92),   "AU": ("AUD", 1.53),  "MX": ("MXN", 17.0),
    "JP": ("JPY", 149.0),  "KR": ("KRW", 1320),  "NG": ("NGN", 780),
    "ZA": ("ZAR", 18.5),   "AR": ("ARS", 350),   "SG": ("SGD", 1.34),
}

query_templates = {
    "title":   lambda: fake.catch_phrase().title(),
    "genre":   lambda: random.choice(PRIMARY_GENRES + MOODS),
    "actor":   lambda: fake.name(),
    "keyword": lambda: random.choice(["best of 2023", "award winning", "based on book",
                                      "new releases", "top 10", "feel good", "dark",
                                      "limited series", "true crime", "foreign"]),
    "mood":    lambda: random.choice(MOODS + ["something funny", "something scary",
                                              "something short", "family night"]),
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def uid() -> str:
    return str(uuid.uuid4())

def weighted_choice(d: dict) -> str:
    return random.choices(list(d.keys()), weights=list(d.values()), k=1)[0]

def rand_date(start: date, end: date) -> date:
    delta = (end - start).days
    if delta <= 0:
        return start
    return start + timedelta(days=random.randint(0, delta))

def rand_ts(start_dt: datetime, end_dt: datetime) -> datetime:
    delta = int((end_dt - start_dt).total_seconds())
    return start_dt + timedelta(seconds=random.randint(0, max(delta, 1)))

def hour_of_day_weight() -> int:
    weights = [
        0.3, 0.2, 0.15, 0.12, 0.12, 0.15,
        0.2, 0.3, 0.4,  0.45, 0.45, 0.4,
        0.5, 0.55, 0.6, 0.65, 0.7, 0.8,
        1.0, 1.4, 1.5,  1.4,  1.1, 0.6,
    ]
    return random.choices(range(24), weights=weights, k=1)[0]

def weekend_multiplier(d: date) -> float:
    return 1.35 if d.weekday() >= 5 else 1.0

def cohort_signup_date(sim_start: date, sim_end: date) -> date:
    weights = []
    d = sim_start
    while d <= sim_end:
        elapsed = (d - sim_start).days
        w = 1 / (1 + math.exp(-0.008 * (elapsed - 300)))
        if d.month in (11, 12):
            w *= 1.4
        if d.month == 1:
            w *= 0.75
        weights.append(w)
        d += timedelta(days=1)
    days_range = [(sim_start + timedelta(days=i)) for i in range(len(weights))]
    return random.choices(days_range, weights=weights, k=1)[0]

def algo_for_date(d: date) -> str:
    if d < date(2023, 7, 1): return "v1"
    if d < date(2024, 4, 1): return "v2"
    return "v3"


# ── Existing-data loaders (READ real IDs from Snowflake — no regeneration) ─────
#
# WHY THIS REPLACED _rebuild_stateful / _rebuild_content:
#   The previous version regenerated all subscribers and content with fixed seeds
#   (Faker.seed / random.seed / np.random.seed), assuming that reproduced the
#   original UUIDs. It does not: uuid.uuid4() draws from os.urandom and ignores
#   random.seed(), so every run minted BRAND-NEW ids. The appended month's events
#   then referenced subscriber/content ids that exist nowhere in the dimensions
#   (0% content-join, ~2% subscriber-join), which is what broke every dimensional
#   breakdown for that month.
#
#   The correct incremental pattern: READ the existing ids from RAW and reference
#   them, only minting new ids for genuinely net-new signups (which we also INSERT,
#   so their foreign keys resolve).

NEW_SUBS_PER_MONTH = 500       # net-new signups to create for the month
MONTHLY_CHURN_RATE = 0.03      # fraction of the active base that churns during the month


def _get_sf_connection():
    """Open a Snowflake connection scoped to the RAW schema."""
    import snowflake.connector
    conn = snowflake.connector.connect(
        user=SF_USER, password=SF_PASSWORD, account=SF_ACCOUNT, role=SF_ROLE,
    )
    cur = conn.cursor()
    cur.execute(f"USE DATABASE {SF_DATABASE};")
    cur.execute(f"USE SCHEMA {SF_SCHEMA};")
    cur.execute(f"USE WAREHOUSE {SF_WAREHOUSE};")
    cur.close()
    return conn


def _iso(v):
    """Normalise a Snowflake date/datetime/None to an ISO string (or None)."""
    if v is None or v == "":
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _load_existing_subscribers(conn) -> dict:
    """Return {subscriber_id: attributes} for every existing subscriber in RAW."""
    cols = ["subscriber_id", "email", "country", "signup_date", "acquisition_channel",
            "plan_type", "plan_price_usd", "subscription_status", "trial_start_date",
            "trial_end_date", "churn_date", "churn_reason", "age_group", "device_preference"]
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(cols)} FROM SUBSCRIBERS")
    sub_info = {}
    for row in cur.fetchall():
        r = dict(zip(cols, row))
        for dc in ("signup_date", "trial_start_date", "trial_end_date", "churn_date"):
            r[dc] = _iso(r[dc])
        r["plan_price_usd"] = float(r["plan_price_usd"]) if r["plan_price_usd"] is not None else 0.0
        sub_info[r["subscriber_id"]] = r
    cur.close()
    print(f"    loaded {len(sub_info):,} existing subscribers")
    return sub_info


def _load_existing_subscriptions(conn):
    """Return (subscription_rows, subscription_map) for existing subscriptions in RAW."""
    cols = ["subscription_id", "subscriber_id", "plan_type", "plan_price_usd", "billing_cycle",
            "status", "start_date", "end_date", "mrr_usd", "is_trial", "payment_method",
            "cancellation_reason"]
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(cols)} FROM SUBSCRIPTIONS")
    subscription_rows = []
    subscription_map  = defaultdict(list)
    for row in cur.fetchall():
        r = dict(zip(cols, row))
        r["start_date"]     = _iso(r["start_date"])
        r["end_date"]       = _iso(r["end_date"])
        r["plan_price_usd"] = float(r["plan_price_usd"]) if r["plan_price_usd"] is not None else 0.0
        r["mrr_usd"]        = float(r["mrr_usd"]) if r["mrr_usd"] is not None else 0.0
        r["is_trial"]       = bool(r["is_trial"])
        subscription_rows.append(r)
        subscription_map[r["subscriber_id"]].append(r)
    cur.close()
    print(f"    loaded {len(subscription_rows):,} existing subscriptions")
    return subscription_rows, subscription_map


def _load_existing_content(conn):
    """Return (content_ids, content_weights, content_runtime) from RAW.CONTENT_CATALOG."""
    cur = conn.cursor()
    cur.execute("SELECT content_id, avg_runtime_minutes FROM CONTENT_CATALOG")
    content_ids     = []
    content_runtime = {}
    for cid, runtime in cur.fetchall():
        content_ids.append(cid)
        content_runtime[cid] = int(runtime) if runtime is not None else 60
    cur.close()
    # Per-item popularity isn't stored in the catalog; approximate the original
    # power-law skew so session/rec content selection stays realistically long-tailed.
    content_weights = [float(np.random.power(0.3)) for _ in content_ids]
    print(f"    loaded {len(content_ids):,} existing content items")
    return content_ids, content_weights, content_runtime


def _load_monthly_session_counts(conn, month_start: date) -> list[tuple[str, int]]:
    """Trailing monthly session counts from RAW (months strictly before month_start)."""
    cur = conn.cursor()
    cur.execute(f"""
        SELECT TO_CHAR(DATE_TRUNC('month', session_start::timestamp), 'YYYY-MM') AS mo,
               COUNT(*)
        FROM STREAM_SESSIONS
        WHERE session_start::timestamp < '{month_start.isoformat()}'
        GROUP BY 1 ORDER BY 1
    """)
    rows = [(m, int(c)) for m, c in cur.fetchall() if m is not None]
    cur.close()
    return rows[-6:]   # last 6 months is enough to estimate the trend


def _session_volume_target(history: list[tuple[str, int]]) -> int | None:
    """
    Realistic session count for the new month: previous month grown by the
    trailing average month-over-month rate (clamped to 0–10% so one noisy
    month can't produce an absurd target). Returns None when there is no
    history to calibrate against (caller falls back to uncalibrated rates).

    WHY: the raw per-subscriber engagement rates in this script imply ~16
    sessions/sub/month, which is ~2x what the base generator's tenure-spread
    logic actually produced historically. Uncalibrated, an appended month
    lands at ~2x the previous month — an obvious synthetic-data artifact on
    the sessions trend chart.
    """
    if not history:
        return None
    counts = [c for _, c in history]
    prev = counts[-1]
    if len(counts) >= 2:
        growths = [counts[i] / counts[i - 1] - 1 for i in range(1, len(counts)) if counts[i - 1] > 0]
        g = sum(growths) / len(growths) if growths else 0.05
    else:
        g = 0.05
    g = min(max(g, 0.0), 0.10)
    return int(prev * (1 + g))


def _generate_new_subscribers(month_start: date, month_end: date):
    """
    Create NET-NEW signups for the month (fresh uuids). These are genuinely new
    rows, INSERTed downstream, so their foreign keys are valid. Returns
    (new_subscriber_rows, new_subscription_rows).
    """
    age_groups  = ["18-24", "25-34", "35-44", "45-54", "55+"]
    age_weights = [0.18, 0.32, 0.25, 0.15, 0.10]

    new_subs          = []
    new_subscriptions = []

    for _ in range(NEW_SUBS_PER_MONTH):
        sid         = uid()
        signup      = rand_date(month_start, month_end)
        plan        = random.choices(PLAN_NAMES, weights=PLAN_WEIGHTS, k=1)[0]
        is_trial    = random.random() < 0.22
        trial_start = signup if is_trial else None
        trial_end   = (signup + timedelta(days=30)) if is_trial else None

        new_subs.append({
            "subscriber_id":       sid,
            "email":               fake.unique.email(),
            "country":             weighted_choice(COUNTRIES),
            "signup_date":         signup.isoformat(),
            "acquisition_channel": weighted_choice(ACQ_CHANNELS),
            "plan_type":           plan,
            "plan_price_usd":      PLAN_PRICES[plan],
            "subscription_status": "active",
            "trial_start_date":    trial_start.isoformat() if trial_start else None,
            "trial_end_date":      trial_end.isoformat()   if trial_end   else None,
            "churn_date":          None,
            "churn_reason":        None,
            "age_group":           random.choices(age_groups, weights=age_weights, k=1)[0],
            "device_preference":   random.choices(DEVICES, weights=[0.40, 0.35, 0.15, 0.10], k=1)[0],
        })

        if is_trial:
            new_subscriptions.append({
                "subscription_id":     uid(),
                "subscriber_id":       sid,
                "plan_type":           plan,
                "plan_price_usd":      0.00,
                "billing_cycle":       "monthly",
                "status":              "cancelled",
                "start_date":          signup.isoformat(),
                "end_date":            trial_end.isoformat(),
                "mrr_usd":             0.00,
                "is_trial":            True,
                "payment_method":      random.choice(PAYMENT_METHODS),
                "cancellation_reason": None,
            })
            start = trial_end
        else:
            start = signup

        billing_cycle = random.choices(["monthly", "annual"], weights=[0.72, 0.28], k=1)[0]
        price = PLAN_PRICES[plan]
        mrr   = price if billing_cycle == "monthly" else round(price * 12 * 0.85 / 12, 2)
        new_subscriptions.append({
            "subscription_id":     uid(),
            "subscriber_id":       sid,
            "plan_type":           plan,
            "plan_price_usd":      price if billing_cycle == "monthly" else round(price * 12 * 0.85, 2),
            "billing_cycle":       billing_cycle,
            "status":              "active",
            "start_date":          start.isoformat(),
            "end_date":            None,
            "mrr_usd":             mrr,
            "is_trial":            False,
            "payment_method":      random.choice(PAYMENT_METHODS),
            "cancellation_reason": None,
        })

    print(f"    generated {len(new_subs):,} new subscribers for the month")
    return new_subs, new_subscriptions


def _apply_month_churn(sub_info: dict, subscription_map: dict,
                       month_start: date, month_end: date):
    """
    Flip a realistic share of currently-active subscribers to 'churned' within the
    month. Mutates sub_info in place (so the churn-updates MERGE picks them up) AND
    updates their subscription rows (end_date/status) so the churn flows into
    fct_mrr_monthly. Returns the list of updated subscription rows to MERGE.
    """
    active = [
        sid for sid, i in sub_info.items()
        if i["subscription_status"] == "active" and not i["churn_date"]
        and i["signup_date"] and date.fromisoformat(i["signup_date"]) < month_start
    ]
    n = min(int(len(active) * MONTHLY_CHURN_RATE), len(active))
    churned_subscription_updates = []

    for sid in random.sample(active, n):
        cdate  = rand_date(month_start, month_end)
        reason = random.choices(CHURN_REASONS, weights=[0.28, 0.22, 0.18, 0.15, 0.10, 0.07], k=1)[0]
        sub_info[sid]["churn_date"]          = cdate.isoformat()
        sub_info[sid]["subscription_status"] = "churned"
        sub_info[sid]["churn_reason"]        = reason
        # End the subscriber's live (non-trial, not-yet-ended) subscription(s).
        for sub in subscription_map.get(sid, []):
            if not sub["is_trial"] and sub["end_date"] is None:
                sub["status"]              = "cancelled"
                sub["end_date"]            = cdate.isoformat()
                sub["cancellation_reason"] = reason
                churned_subscription_updates.append(sub)

    print(f"    churned {n:,} existing subscribers this month")
    return churned_subscription_updates


# ── Core incremental generators ───────────────────────────────────────────────

def generate_month(month_start: date, month_end: date, preview: bool = False) -> dict[str, pd.DataFrame]:
    """
    Generate all incremental rows for [month_start, month_end].
    Returns a dict of {table_name: DataFrame}.
    """
    print(f"\nLoading existing subscribers / subscriptions / content from Snowflake...")
    _conn = _get_sf_connection()
    try:
        sub_info = _load_existing_subscribers(_conn)
        subscription_rows, subscription_map = _load_existing_subscriptions(_conn)
        content_ids, content_weights, content_runtime = _load_existing_content(_conn)
        session_history = _load_monthly_session_counts(_conn, month_start)
    finally:
        _conn.close()

    if not sub_info or not content_ids:
        raise RuntimeError(
            "No existing subscribers/content found in RAW. Load the base dataset "
            "(generate_streaming_data.py) before appending an incremental month."
        )

    # Net-new signups for the month (fresh ids — INSERTed downstream so FKs resolve).
    new_subs, new_subscriptions = _generate_new_subscribers(month_start, month_end)
    for s in new_subs:
        sub_info[s["subscriber_id"]] = s
    for s in new_subscriptions:
        subscription_rows.append(s)
        subscription_map[s["subscriber_id"]].append(s)
    new_sub_ids = {s["subscriber_id"] for s in new_subs}

    # New churn events for the month (existing actives → churned within the window).
    # Mutates sub_info + subscription rows in place; returns the subscription
    # updates that must be MERGEd so the churn flows into fct_mrr_monthly.
    churned_subscription_updates = _apply_month_churn(
        sub_info, subscription_map, month_start, month_end
    )

    results: dict[str, pd.DataFrame] = {}

    # ── Payments ──────────────────────────────────────────────────────────────
    print("  Generating payments...")
    payment_rows = []
    for sub in subscription_rows:
        if sub["is_trial"]:
            continue
        sid    = sub["subscriber_id"]
        sub_id = sub["subscription_id"]
        cycle  = sub["billing_cycle"]
        price  = sub["plan_price_usd"]
        start  = date.fromisoformat(sub["start_date"])
        end    = date.fromisoformat(sub["end_date"]) if sub["end_date"] else month_end
        method = sub["payment_method"]

        interval_days = 30 if cycle == "monthly" else 365
        pay_date      = start
        is_first      = True

        while pay_date <= min(end, month_end):
            if month_start <= pay_date <= month_end:
                period_end   = pay_date + timedelta(days=interval_days - 1)
                failed       = random.random() < 0.035
                refunded     = (not failed) and random.random() < 0.008
                disputed     = (not failed) and (not refunded) and random.random() < 0.003
                pay_status   = "failed" if failed else ("refunded" if refunded else ("disputed" if disputed else "succeeded"))
                fail_reason  = random.choice(FAILURE_REASONS) if failed else None
                discount     = is_first and random.random() < 0.18
                discount_pct = round(random.choice([0.10, 0.20, 0.30, 0.50]), 2) if discount else None
                final_amount = round(price * (1 - discount_pct), 2) if discount else price
                country      = sub_info[sid]["country"]
                curr, fx     = currency_map.get(country, ("USD", 1.0))

                if failed:
                    retry_date = pay_date + timedelta(days=3)
                    if retry_date <= min(end, month_end):
                        retry_ok = random.random() < 0.60
                        payment_rows.append({
                            "payment_id":           uid(),
                            "subscription_id":      sub_id,
                            "subscriber_id":        sid,
                            "payment_date":         retry_date.isoformat(),
                            "billing_period_start": pay_date.isoformat(),
                            "billing_period_end":   period_end.isoformat(),
                            "amount_usd":           price,
                            "currency":             curr,
                            "amount_local":         round(price * fx, 2),
                            "status":               "succeeded" if retry_ok else "failed",
                            "failure_reason":       None if retry_ok else random.choice(FAILURE_REASONS),
                            "payment_method":       method,
                            "stripe_charge_id":     f"ch_{uid().replace('-','')[:24]}",
                            "is_renewal":           not is_first,
                            "is_retry":             True,
                            "discount_applied":     False,
                            "discount_pct":         None,
                        })

                payment_rows.append({
                    "payment_id":           uid(),
                    "subscription_id":      sub_id,
                    "subscriber_id":        sid,
                    "payment_date":         pay_date.isoformat(),
                    "billing_period_start": pay_date.isoformat(),
                    "billing_period_end":   period_end.isoformat(),
                    "amount_usd":           final_amount,
                    "currency":             curr,
                    "amount_local":         round(final_amount * fx, 2),
                    "status":               pay_status,
                    "failure_reason":       fail_reason,
                    "payment_method":       method,
                    "stripe_charge_id":     f"ch_{uid().replace('-','')[:24]}",
                    "is_renewal":           not is_first,
                    "is_retry":             False,
                    "discount_applied":     discount,
                    "discount_pct":         discount_pct,
                })
            pay_date += timedelta(days=interval_days)
            is_first  = False

    results["payments"] = pd.DataFrame(payment_rows)

    # ── Stream Sessions ────────────────────────────────────────────────────────
    print("  Generating stream_sessions...")
    session_rows       = []
    session_ids_by_sub = defaultdict(list)
    active_subs        = [
        sid for sid, info in sub_info.items()
        if info["subscription_status"] in ("active", "paused", "churned")
    ]

    # First pass: eligibility + engagement rates, so total volume can be
    # calibrated to the warehouse trend BEFORE any sessions are drawn.
    eligible: list[tuple[str, date, date, str, int]] = []
    for sid in active_subs:
        info       = sub_info[sid]
        signup     = date.fromisoformat(info["signup_date"])
        churn_d    = date.fromisoformat(info["churn_date"]) if info["churn_date"] else month_end
        active_end = min(churn_d, month_end)

        # Clamp to the month window
        sess_start = max(signup, month_start)
        if sess_start > active_end:
            continue

        engagement_level = random.choices(
            ["heavy", "medium", "light", "very_light"],
            weights=[0.15, 0.40, 0.30, 0.15], k=1)[0]
        monthly_rate = {"heavy": 45, "medium": 18, "light": 6, "very_light": 2}[engagement_level]
        eligible.append((sid, sess_start, active_end, engagement_level, monthly_rate))

    expected_total = sum(rate for *_, rate in eligible)
    target_total   = _session_volume_target(session_history)
    volume_scale   = 1.0
    if target_total and expected_total > 0:
        volume_scale = target_total / expected_total
        print(f"    calibrating session volume to warehouse trend: "
              f"target {target_total:,} (prev months: "
              f"{', '.join(f'{m}={c:,}' for m, c in session_history[-3:])}), "
              f"uncalibrated expectation {expected_total:,}, scale {volume_scale:.2f}")
    else:
        print("    [!] no session history found — generating with uncalibrated rates")

    for sid, sess_start, active_end, engagement_level, monthly_rate in eligible:
        info = sub_info[sid]
        n_sessions = max(1, int(np.random.poisson(monthly_rate * volume_scale)))

        country = info["country"]
        device  = info["device_preference"]

        for _ in range(n_sessions):
            session_date = rand_date(sess_start, active_end)
            hour         = hour_of_day_weight()
            start_dt     = datetime(
                session_date.year, session_date.month, session_date.day,
                hour, random.randint(0, 59), random.randint(0, 59)
            )
            cid     = random.choices(content_ids, weights=content_weights, k=1)[0]
            runtime = content_runtime[cid]
            completion_base = {"heavy": 0.78, "medium": 0.58, "light": 0.42, "very_light": 0.30}[engagement_level]
            completion_pct  = min(1.0, max(0.01, np.random.beta(
                completion_base * 5, (1 - completion_base) * 5
            )))
            watched_mins = round(runtime * completion_pct, 1)
            end_dt       = start_dt + timedelta(minutes=watched_mins)
            plan         = info["plan_type"]
            qual_w       = {"basic": [0.6, 0.35, 0.05],
                            "standard": [0.15, 0.65, 0.20],
                            "premium":  [0.05, 0.35, 0.60]}[plan]
            quality      = random.choices(QUALITY_LEVELS, weights=qual_w, k=1)[0]
            buffer_base  = {"SD": 0.1, "HD": 0.25, "4K": 0.45}[quality]
            buf_events   = np.random.poisson(buffer_base * (watched_mins / 10))
            sess_id      = uid()
            session_rows.append({
                "session_id":          sess_id,
                "subscriber_id":       sid,
                "content_id":          cid,
                "session_start":       start_dt.isoformat(),
                "session_end":         end_dt.isoformat(),
                "duration_minutes":    watched_mins,
                "content_runtime_min": runtime,
                "completion_pct":      round(completion_pct, 4),
                "device_type":         device if random.random() < 0.75 else random.choice(DEVICES),
                "country":             country,
                "quality_streamed":    quality,
                "buffering_events":    int(buf_events),
                "was_resumed":         random.random() < 0.18,
                "referral_source":     random.choices(
                                           ["home_page", "search", "recommendation",
                                            "continue_watching", "external"],
                                           weights=[0.25, 0.15, 0.35, 0.20, 0.05], k=1)[0],
            })
            session_ids_by_sub[sid].append(sess_id)

    results["stream_sessions"] = pd.DataFrame(session_rows)

    # ── Recommendation Events ──────────────────────────────────────────────────
    print("  Generating recommendation_events...")
    ctr_by_algo = {"v1": 0.08, "v2": 0.12, "v3": 0.17}
    rec_rows    = []
    TARGET_RECS = max(1, int(200_000 / 40))  # ~1 month / 40 months of history

    subs_for_recs = random.choices(
        list(session_ids_by_sub.keys()),
        k=min(TARGET_RECS * 5, len(session_ids_by_sub) * 15) if session_ids_by_sub else 1
    )

    for sid in subs_for_recs:
        if len(rec_rows) >= TARGET_RECS:
            break
        info    = sub_info[sid]
        signup  = date.fromisoformat(info["signup_date"])
        churn_d = date.fromisoformat(info["churn_date"]) if info["churn_date"] else month_end
        rec_date = rand_date(max(signup, month_start), min(churn_d, month_end))
        algo     = algo_for_date(rec_date)
        ctr      = ctr_by_algo[algo]
        rec_type = random.choice(REC_TYPES)
        n_shown  = random.randint(5, 15)
        shown_content = random.choices(content_ids, weights=content_weights, k=n_shown)

        for pos, cid in enumerate(shown_content, 1):
            was_clicked   = random.random() < (ctr / pos ** 0.4)
            was_streamed_ = False
            sess_link     = None
            if was_clicked:
                was_streamed_ = random.random() < 0.52
                if was_streamed_:
                    subs_sessions = session_ids_by_sub.get(sid, [])
                    sess_link     = random.choice(subs_sessions) if subs_sessions else None
            rec_ts = datetime(rec_date.year, rec_date.month, rec_date.day,
                              hour_of_day_weight(), random.randint(0, 59))
            rec_rows.append({
                "event_id":            uid(),
                "subscriber_id":       sid,
                "content_id":          cid,
                "event_timestamp":     rec_ts.isoformat(),
                "recommendation_type": rec_type,
                "position_shown":      pos,
                "was_clicked":         was_clicked,
                "was_streamed":        was_streamed_,
                "session_id":          sess_link,
                "algorithm_version":   algo,
            })
            if len(rec_rows) >= TARGET_RECS:
                break

    results["recommendation_events"] = pd.DataFrame(rec_rows[:TARGET_RECS])

    # ── Search Events ──────────────────────────────────────────────────────────
    print("  Generating search_events...")
    search_rows     = []
    TARGET_SEARCHES = max(1, int(100_000 / 40))

    for _ in range(TARGET_SEARCHES):
        sid    = random.choice(list(sub_info.keys()))
        info   = sub_info[sid]
        signup = date.fromisoformat(info["signup_date"])
        churn_d= date.fromisoformat(info["churn_date"]) if info["churn_date"] else month_end
        s_date = rand_date(max(signup, month_start), min(churn_d, month_end))
        stype  = random.choices(list(query_templates.keys()),
                                weights=[0.30, 0.25, 0.15, 0.20, 0.10], k=1)[0]
        query  = query_templates[stype]()
        results_n = random.randint(0, 50)
        clicked   = results_n > 0 and random.random() < 0.62
        clicked_pos = random.randint(1, min(results_n, 10)) if clicked else None
        cid_clicked = random.choice(content_ids) if clicked else None
        sess_started = clicked and random.random() < 0.48
        search_rows.append({
            "search_id":          uid(),
            "subscriber_id":      sid,
            "search_timestamp":   datetime(s_date.year, s_date.month, s_date.day,
                                           hour_of_day_weight(), random.randint(0, 59)).isoformat(),
            "query_text":         query,
            "query_type":         stype,
            "results_returned":   results_n,
            "clicked_position":   clicked_pos,
            "content_id_clicked": cid_clicked,
            "session_started":    sess_started,
            "device_type":        random.choice(DEVICES),
        })

    results["search_events"] = pd.DataFrame(search_rows)

    # ── User Watchlists ────────────────────────────────────────────────────────
    print("  Generating user_watchlists...")
    watchlist_rows = []
    TARGET_WL      = max(1, int(50_000 / 40))
    sources        = ["browse", "recommendation", "search", "share", "trailer"]

    for _ in range(TARGET_WL):
        sid    = random.choice(list(sub_info.keys()))
        info   = sub_info[sid]
        signup = date.fromisoformat(info["signup_date"])
        churn_d= date.fromisoformat(info["churn_date"]) if info["churn_date"] else month_end
        add_d  = rand_date(max(signup, month_start), min(churn_d, month_end))
        cid    = random.choices(content_ids, weights=content_weights, k=1)[0]
        removed   = random.random() < 0.35
        remove_ts = None
        if removed:
            remove_lo = add_d + timedelta(days=1)
            remove_hi = min(churn_d, month_end)
            if remove_lo < remove_hi:
                remove_d  = rand_date(remove_lo, remove_hi)
                remove_ts = datetime(remove_d.year, remove_d.month, remove_d.day).isoformat()
        was_streamed = random.random() < 0.42
        linked_sess  = None
        if was_streamed:
            subs_sessions = session_ids_by_sub.get(sid, [])
            linked_sess   = random.choice(subs_sessions) if subs_sessions else None
        watchlist_rows.append({
            "watchlist_id":      uid(),
            "subscriber_id":     sid,
            "content_id":        cid,
            "added_timestamp":   datetime(add_d.year, add_d.month, add_d.day,
                                          random.randint(0, 23), random.randint(0, 59)).isoformat(),
            "removed_timestamp": remove_ts,
            "was_streamed":      was_streamed,
            "stream_session_id": linked_sess,
            "source":            random.choices(
                                     sources, weights=[0.30, 0.28, 0.20, 0.12, 0.10], k=1)[0],
        })

    results["user_watchlists"] = pd.DataFrame(watchlist_rows)

    # ── New Subscribers & Subscription changes for this month ─────────────────
    # New subscriber rows (net-new signups) → INSERT via COPY INTO.
    # Churned-subscriber subscription updates → MERGE (so churn flows into MRR).
    print("  Assembling new subscribers / subscription changes for this month...")
    new_subscriber_rows = [sub_info[sid] for sid in new_sub_ids]
    results["subscribers"] = pd.DataFrame(new_subscriber_rows)

    new_subscription_rows = [
        row for row in subscription_rows if row["subscriber_id"] in new_sub_ids
    ]
    # MERGE payload = new subscriptions (INSERT) + churned existing ones (UPDATE end_date/status).
    results["subscriptions"] = pd.DataFrame(new_subscription_rows + churned_subscription_updates)
    print(f"    -> {len(new_subscriber_rows):,} new subscribers, "
          f"{len(new_subscription_rows):,} new + {len(churned_subscription_updates):,} churned-updated subscriptions")

    # ── Churned Subscriber Updates for this month ──────────────────────────────
    # Existing subscribers whose churn_date falls within this month.
    # These need a MERGE (UPDATE) in Snowflake — collected separately so
    # upload_to_snowflake() can apply the correct MERGE strategy.
    print("  Collecting churned subscriber updates...")
    churned_update_rows = [
        row for row in list(sub_info.values())
        if row["churn_date"]
        and month_start <= date.fromisoformat(row["churn_date"]) <= month_end
        and row["subscriber_id"] not in new_sub_ids  # already covered above
    ]
    results["subscribers_churn_updates"] = pd.DataFrame(churned_update_rows)
    print(f"    -> {len(churned_update_rows):,} subscribers churned this month (will MERGE)")

    # ── Subscription Plan History (plan changes this month) ────────────────────
    # ~5% of existing active subscribers change their plan each month.
    # This drives Expansion and Contraction in fct_mrr_monthly.
    print("  Generating subscription_plan_history...")
    plan_change_rows = []
    existing_active = [
        sid for sid, info in sub_info.items()
        if info["subscription_status"] == "active"
        and date.fromisoformat(info["signup_date"]) < month_start
    ]
    n_changes = int(len(existing_active) * 0.05)
    changers = random.sample(existing_active, min(n_changes, len(existing_active)))

    for sid in changers:
        info    = sub_info[sid]
        old_plan = info["plan_type"]
        other_plans = [p for p in PLAN_NAMES if p != old_plan]
        new_plan = random.choice(other_plans)
        change_direction = "upgrade" if PLAN_ORDER[new_plan] > PLAN_ORDER[old_plan] else "downgrade"
        change_d  = rand_date(month_start, month_end)
        billing_cycle = random.choices(["monthly", "annual"], weights=[0.72, 0.28], k=1)[0]
        old_mrr = PLAN_PRICES[old_plan] if billing_cycle == "monthly" else round(PLAN_PRICES[old_plan] * 12 * 0.85 / 12, 2)
        new_mrr = PLAN_PRICES[new_plan] if billing_cycle == "monthly" else round(PLAN_PRICES[new_plan] * 12 * 0.85 / 12, 2)
        plan_change_rows.append({
            "change_id":      uid(),
            "subscriber_id":  sid,
            "change_date":    change_d.isoformat(),
            "old_plan":       old_plan,
            "new_plan":       new_plan,
            "change_type":    change_direction,
            "old_mrr_usd":    old_mrr,
            "new_mrr_usd":    new_mrr,
            "change_reason":  random.choice(["voluntary", "promotional", "annual_renewal"]),
        })

    results["subscription_plan_history"] = pd.DataFrame(plan_change_rows)
    print(f"    -> {len(plan_change_rows):,} plan changes generated")

    return results


# ── CSV append ────────────────────────────────────────────────────────────────

def append_to_csv(dfs: dict[str, pd.DataFrame], month_label: str) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for table, df in dfs.items():
        if df.empty:
            print(f"  [skip] {table}: 0 rows generated")
            continue
        path = os.path.join(OUTPUT_DIR, f"{table}.csv")
        exists = os.path.exists(path)
        df.to_csv(path, mode="a", header=not exists, index=False)
        print(f"  [csv]  {table}: appended {len(df):,} rows -> {path}")


# ── Snowflake upload ──────────────────────────────────────────────────────────

# Tables that must be MERGEd (UPDATE existing rows) rather than simply INSERTed.
# Key: raw table name  →  Value: merge key column(s)
_MERGE_TABLES: dict[str, list[str]] = {
    "subscriptions":              ["subscription_id"],
    "subscription_plan_history":  ["change_id"],
}

# Special pseudo-table used to MERGE churn updates back into subscribers.
_CHURN_UPDATE_TABLE = "subscribers_churn_updates"

def upload_to_snowflake(dfs: dict[str, pd.DataFrame], month_label: str) -> None:
    import snowflake.connector

    print(f"\nConnecting to Snowflake ({SF_ACCOUNT}) as {SF_USER}...")
    conn = snowflake.connector.connect(
        user=SF_USER,
        password=SF_PASSWORD,
        account=SF_ACCOUNT,
        role=SF_ROLE,
    )
    cur = conn.cursor()

    cur.execute(f"USE DATABASE {SF_DATABASE};")
    cur.execute(f"USE SCHEMA {SF_SCHEMA};")
    cur.execute(f"USE WAREHOUSE {SF_WAREHOUSE};")

    # Ensure the staging area and file format exist
    cur.execute("CREATE STAGE IF NOT EXISTS INCREMENTAL_STAGE;")
    cur.execute("""
        CREATE FILE FORMAT IF NOT EXISTS INCREMENTAL_CSV_FORMAT
            TYPE = 'CSV'
            FIELD_OPTIONALLY_ENCLOSED_BY = '"'
            SKIP_HEADER = 1
            NULL_IF = ('', 'None', 'NULL');
    """)

    for table, df in dfs.items():
        if df.empty:
            print(f"  [skip] {table}: nothing to upload")
            continue

        # Write temp CSV
        tmp_path = os.path.join(OUTPUT_DIR, f"_incremental_{table}_{month_label}.csv")
        df.to_csv(tmp_path, index=False)
        sf_path  = tmp_path.replace("\\", "/")
        fname    = os.path.basename(tmp_path)

        cols     = df.columns.tolist()
        cols_sql = ", ".join(cols)
        col_refs = ", ".join([f"${i + 1}" for i in range(len(cols))])

        print(f"\n  [{table}] uploading {len(df):,} rows ({len(cols)} columns)...")
        cur.execute(f"PUT file://{sf_path} @INCREMENTAL_STAGE/ OVERWRITE = TRUE;")

        # ── Handle churn updates: MERGE into SUBSCRIBERS to flip status & churn_date ──
        if table == _CHURN_UPDATE_TABLE:
            tmp_tbl = f"TMP_CHURN_UPDATES_{month_label.replace('-', '_')}"
            cur.execute(f"""
                CREATE OR REPLACE TEMPORARY TABLE {tmp_tbl} ({cols_sql} VARCHAR)
                AS
                SELECT {col_refs}
                FROM @INCREMENTAL_STAGE/{fname}
                (FILE_FORMAT => INCREMENTAL_CSV_FORMAT);
            """)
            cur.execute(f"""
                MERGE INTO SUBSCRIBERS tgt
                USING {tmp_tbl} src
                ON tgt.subscriber_id = src.subscriber_id
                WHEN MATCHED THEN UPDATE SET
                    tgt.subscription_status = src.subscription_status,
                    tgt.churn_date          = TRY_TO_DATE(src.churn_date),
                    tgt.churn_reason        = src.churn_reason;
            """)
            print(f"  [{table}] MERGEd churn status into SUBSCRIBERS [ok]")

        # ── MERGE for stateful tables (subscriptions, subscription_plan_history) ──
        elif table in _MERGE_TABLES:
            merge_keys = _MERGE_TABLES[table]
            tmp_tbl    = f"TMP_{table.upper()}_{month_label.replace('-', '_')}"
            cur.execute(f"""
                CREATE OR REPLACE TEMPORARY TABLE {tmp_tbl} ({cols_sql} VARCHAR)
                AS
                SELECT {col_refs}
                FROM @INCREMENTAL_STAGE/{fname}
                (FILE_FORMAT => INCREMENTAL_CSV_FORMAT);
            """)
            on_clause      = " AND ".join(f"tgt.{k} = src.{k}" for k in merge_keys)
            update_clause  = ", ".join(
                f"tgt.{c} = src.{c}" for c in cols if c not in merge_keys
            )
            insert_cols    = cols_sql
            insert_vals    = ", ".join(f"src.{c}" for c in cols)
            cur.execute(f"""
                MERGE INTO {table.upper()} tgt
                USING {tmp_tbl} src
                ON {on_clause}
                WHEN MATCHED THEN UPDATE SET {update_clause}
                WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals});
            """)
            print(f"  [{table}] MERGEd into {table.upper()} [ok]")

        # ── Simple INSERT (append-only event tables + new subscribers) ──────────
        else:
            cur.execute(f"""
                COPY INTO {table.upper()} ({cols_sql})
                FROM (
                    SELECT {col_refs}
                    FROM @INCREMENTAL_STAGE/{fname}
                )
                FILE_FORMAT = (FORMAT_NAME = INCREMENTAL_CSV_FORMAT)
                PURGE = TRUE;
            """)
            print(f"  [{table}] inserted into {table.upper()} [ok]")

        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    cur.close()
    conn.close()
    print("\nSnowflake upload complete.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_month(s: str) -> tuple[date, date]:
    """Parse 'YYYY-MM' -> (first_day, last_day) of that month."""
    try:
        y, m = int(s[:4]), int(s[5:7])
    except (ValueError, IndexError):
        raise argparse.ArgumentTypeError(f"Invalid month format '{s}'. Use YYYY-MM (e.g. 2026-06).")
    first = date(y, m, 1)
    if m == 12:
        last = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(y, m + 1, 1) - timedelta(days=1)
    return first, last


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Append one or more months of incremental data to CSVs and Snowflake."
    )
    parser.add_argument(
        "--month", action="append", required=True, metavar="YYYY-MM",
        help="Month to generate (e.g. 2026-06). Repeat to process multiple months."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate and append CSVs only -- skip Snowflake upload."
    )
    parser.add_argument(
        "--preview", action="store_true",
        help="Print row counts only -- do not write any files."
    )
    args = parser.parse_args()

    months = []
    for m in args.month:
        first, last = _parse_month(m)
        months.append((m, first, last))

    for label, month_start, month_end in months:
        today = date.today()
        if month_end >= today:
            print(f"\n[!] Warning: {label} hasn't ended yet (last day is {month_end}). "
                  f"Data will be partial. Continue anyway? [y/N] ", end="")
            if input().strip().lower() != "y":
                print("  Skipped.")
                continue

        print(f"\n{'='*60}")
        print(f"  Processing month: {label}  ({month_start} -> {month_end})")
        print(f"{'='*60}")

        dfs = generate_month(month_start, month_end, preview=args.preview)

        # Summary
        print(f"\n  Row counts for {label}:")
        total_new = 0
        for table, df in dfs.items():
            print(f"    {table:<32} {len(df):>8,} rows")
            total_new += len(df)
        print(f"    {'TOTAL':<32} {total_new:>8,} rows")

        if args.preview:
            print("\n  [preview mode] No files written.")
            continue

        append_to_csv(dfs, label)

        if not args.dry_run:
            upload_to_snowflake(dfs, label)
        else:
            print("\n  [dry-run] Snowflake upload skipped.")

    print("\nDone.")


if __name__ == "__main__":
    main()
