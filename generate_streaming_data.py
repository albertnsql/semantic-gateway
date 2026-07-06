"""
Streaming Platform Synthetic Data Generator
============================================
Generates 3 years of realistic Netflix-style SaaS data for the
AI-Native Semantic Gateway project.

Tables generated:
  subscribers              ~15,000
  content_catalog           2,500
  content_genre_bridge      ~6,000
  subscriptions            ~18,000
  subscription_plan_history ~8,000
  payments                 ~75,000
  stream_sessions         ~500,000
  recommendation_events   ~200,000
  search_events           ~100,000
  user_watchlists          ~50,000

Output: /output/*.csv

INCREMENTAL MODE
----------------
Set INCREMENTAL_MODE = True and INCREMENTAL_FROM to the first date
you want to append. Stateful tables (subscribers, subscriptions,
subscription_plan_history) are always fully regenerated — they're
small and fast. Event tables (payments, stream_sessions,
recommendation_events, search_events, user_watchlists) are filtered
to INCREMENTAL_FROM onward and appended to existing CSVs.

Fixed seeds ensure subscriber IDs are identical across runs, so
foreign keys in appended event rows remain valid.
"""

import os
import random
import math
import uuid
from datetime import date, datetime, timedelta
from collections import defaultdict

import numpy as np
import pandas as pd
from faker import Faker

fake = Faker()
Faker.seed(42)
random.seed(42)
np.random.seed(42)

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── INCREMENTAL SETTINGS ──────────────────────────────────────────────────────
# Set INCREMENTAL_MODE = True to append new months without regenerating
# everything. Point INCREMENTAL_FROM at the first date you need.
INCREMENTAL_MODE  = False
INCREMENTAL_FROM  = date(2026, 6, 1)   # only used when INCREMENTAL_MODE = True

# ── DATE RANGE ────────────────────────────────────────────────────────────────
SIM_START = date(2023, 1, 1)
SIM_END   = date(2026, 5, 31)          # advance this to extend the dataset
SIM_DAYS  = (SIM_END - SIM_START).days

def rand_date(start=SIM_START, end=SIM_END):
    return start + timedelta(days=random.randint(0, (end - start).days))

def rand_ts(start_dt, end_dt):
    delta = end_dt - start_dt
    secs  = int(delta.total_seconds())
    return start_dt + timedelta(seconds=random.randint(0, max(secs, 1)))

def to_dt(d):
    return datetime(d.year, d.month, d.day)

# ── CONSTANTS ─────────────────────────────────────────────────────────────────
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
# Ordered for directional logic: index 0 = cheapest
PLAN_ORDER   = {"basic": 0, "standard": 1, "premium": 2}

CHURN_REASONS   = ["price", "content_library", "competitor", "involuntary_payment",
                   "technical_issues", "taking_a_break"]
CHANGE_TYPES    = ["upgrade", "downgrade", "reactivation", "pause"]
CONTENT_TYPES   = {"movie": 0.45, "series": 0.35, "documentary": 0.12, "short": 0.08}
MATURITY_RATINGS= ["G", "PG", "PG-13", "R", "TV-MA"]
LANGUAGES       = ["en", "es", "fr", "de", "ja", "ko", "pt", "hi", "ar"]
DEVICES         = ["tv", "mobile", "desktop", "tablet"]
QUALITY_LEVELS  = ["SD", "HD", "4K"]
REC_TYPES       = ["because_you_watched", "trending", "new_release",
                   "top_picks", "continue_watching"]
SEARCH_TYPES    = ["title", "genre", "actor", "keyword", "mood"]
PAYMENT_METHODS = ["card", "paypal", "appstore", "googleplay"]
FAILURE_REASONS = ["insufficient_funds", "card_expired", "fraud_detected",
                   "bank_declined", "invalid_card"]

PRIMARY_GENRES = [
    "drama", "thriller", "comedy", "action", "sci-fi",
    "horror", "romance", "documentary", "animation", "crime",
]
SECONDARY_TAGS = [
    "dark", "feel-good", "based-on-true-story", "award-winning",
    "binge-worthy", "family", "lgbtq+", "international", "cult-classic",
    "mind-bending", "violent", "slow-burn", "witty", "inspirational",
]
MOODS = ["intense", "light-hearted", "thought-provoking", "suspenseful", "emotional"]

DIRECTORS  = [fake.name() for _ in range(300)]
ACTOR_POOL = [fake.name() for _ in range(500)]

# ── HELPERS ───────────────────────────────────────────────────────────────────
def weighted_choice(d):
    keys   = list(d.keys())
    weights= list(d.values())
    return random.choices(keys, weights=weights, k=1)[0]

def uid():
    return str(uuid.uuid4())

def cohort_signup_date():
    """
    Simulate realistic cohort growth:
    - slow ramp Jan–Jun 2023
    - strong growth mid-2023 through 2024
    - plateau + slight churn pressure 2025–2026
    """
    weights = []
    d = SIM_START
    while d <= SIM_END:
        elapsed = (d - SIM_START).days
        # logistic-ish growth curve
        w = 1 / (1 + math.exp(-0.008 * (elapsed - 300)))
        # seasonal bump: Nov-Dec each year
        if d.month in (11, 12):
            w *= 1.4
        # slight dip Jan (post-holiday)
        if d.month == 1:
            w *= 0.75
        weights.append(w)
        d += timedelta(days=1)

    days_range = [(SIM_START + timedelta(days=i)) for i in range(len(weights))]
    return random.choices(days_range, weights=weights, k=1)[0]

def hour_of_day_weight():
    """Peak streaming hours: 7pm-11pm. Lower 8am-12pm."""
    weights = [
        0.3, 0.2, 0.15, 0.12, 0.12, 0.15,   # 0-5
        0.2, 0.3, 0.4,  0.45, 0.45, 0.4,    # 6-11
        0.5, 0.55, 0.6, 0.65, 0.7, 0.8,     # 12-17
        1.0, 1.4, 1.5,  1.4,  1.1, 0.6,     # 18-23
    ]
    return random.choices(range(24), weights=weights, k=1)[0]

def weekend_multiplier(d):
    return 1.35 if d.weekday() >= 5 else 1.0

def save_csv(df, name, incremental=False):
    """Write or append a CSV depending on mode."""
    path = f"{OUTPUT_DIR}/{name}.csv"
    if incremental:
        df.to_csv(path, mode="a", header=not os.path.exists(path), index=False)
        print(f"  {name}: appended {len(df):,} rows → {path}")
    else:
        df.to_csv(path, index=False)
        print(f"  {name}: {len(df):,} rows → {path}")

# ── 1. CONTENT CATALOG ────────────────────────────────────────────────────────
print("Generating content_catalog...")

N_CONTENT = 2500
content_rows = []
content_ids  = []

for _ in range(N_CONTENT):
    cid   = uid()
    ctype = weighted_choice(CONTENT_TYPES)
    genre = random.choice(PRIMARY_GENRES)

    release_year = random.choices(
        range(1990, 2025),
        weights=[max(0.1, 1 + 0.15 * (y - 1990)) for y in range(1990, 2025)],
        k=1
    )[0]

    is_original = random.random() < 0.28

    if ctype == "movie":
        runtime   = random.randint(75, 180)
        seasons   = None
        episodes  = None
    elif ctype == "series":
        runtime   = random.randint(22, 65)
        seasons   = random.randint(1, 8)
        episodes  = seasons * random.randint(6, 13)
    elif ctype == "documentary":
        runtime   = random.randint(45, 120)
        seasons   = None
        episodes  = None
    else:  # short
        runtime   = random.randint(8, 30)
        seasons   = None
        episodes  = None

    # Popularity score (used later to weight session generation)
    # Power-law: most content is obscure, a few titles dominate
    popularity = np.random.power(0.3)

    date_added = rand_date(SIM_START, SIM_END - timedelta(days=30))

    content_rows.append({
        "content_id":          cid,
        "title":               fake.catch_phrase().title(),
        "content_type":        ctype,
        "genre":               genre,
        "subgenre":            random.choice(SECONDARY_TAGS + [None, None]),
        "release_year":        release_year,
        "original_language":   random.choices(
                                   LANGUAGES,
                                   weights=[0.45,0.12,0.08,0.07,0.06,0.06,0.05,0.06,0.05],
                                   k=1)[0],
        "is_original":         is_original,
        "maturity_rating":     random.choices(
                                   MATURITY_RATINGS,
                                   weights=[0.05,0.10,0.30,0.30,0.25], k=1)[0],
        "avg_runtime_minutes": runtime,
        "season_count":        seasons,
        "episode_count":       episodes,
        "director":            random.choice(DIRECTORS),
        "production_country":  weighted_choice(COUNTRIES),
        "date_added_platform": date_added.isoformat(),
        "_popularity":         popularity,  # internal, dropped before export
    })
    content_ids.append(cid)

content_df = pd.DataFrame(content_rows)

# Build O(1) lookup dicts before dropping internal columns
content_popularity = dict(zip(content_df["content_id"], content_df["_popularity"]))
content_runtime    = dict(zip(content_df["content_id"], content_df["avg_runtime_minutes"]))

content_df = content_df.drop(columns=["_popularity"])
# content_catalog is stateful — always full write
content_df.to_csv(f"{OUTPUT_DIR}/content_catalog.csv", index=False)
print(f"  content_catalog: {len(content_df):,} rows")

# ── 2. CONTENT GENRE BRIDGE ───────────────────────────────────────────────────
print("Generating content_genre_bridge...")

bridge_rows = []
for _, row in content_df.iterrows():
    cid     = row["content_id"]
    primary = row["genre"]

    # Primary genre (always present)
    bridge_rows.append({
        "bridge_id":  uid(),
        "content_id": cid,
        "genre":      primary,
        "is_primary": True,
        "tag_type":   "genre",
    })

    # 0–2 secondary genres
    n_secondary = random.choices([0, 1, 2], weights=[0.35, 0.45, 0.20], k=1)[0]
    used = {primary}
    for _ in range(n_secondary):
        g = random.choice(PRIMARY_GENRES)
        if g not in used:
            bridge_rows.append({
                "bridge_id":  uid(),
                "content_id": cid,
                "genre":      g,
                "is_primary": False,
                "tag_type":   "genre",
            })
            used.add(g)

    # 1–3 mood/theme tags
    n_tags = random.randint(1, 3)
    for tag in random.sample(MOODS + SECONDARY_TAGS, n_tags):
        bridge_rows.append({
            "bridge_id":  uid(),
            "content_id": cid,
            "genre":      tag,
            "is_primary": False,
            "tag_type":   "mood" if tag in MOODS else "theme",
        })

bridge_df = pd.DataFrame(bridge_rows)
bridge_df.to_csv(f"{OUTPUT_DIR}/content_genre_bridge.csv", index=False)
print(f"  content_genre_bridge: {len(bridge_df):,} rows")

# ── 3. SUBSCRIBERS ────────────────────────────────────────────────────────────
print("Generating subscribers...")

N_SUBSCRIBERS = 15000
subscriber_rows = []
subscriber_ids  = []

age_groups  = ["18-24", "25-34", "35-44", "45-54", "55+"]
age_weights = [0.18, 0.32, 0.25, 0.15, 0.10]

for _ in range(N_SUBSCRIBERS):
    sid         = uid()
    signup      = cohort_signup_date()
    country     = weighted_choice(COUNTRIES)
    plan        = random.choices(PLAN_NAMES, weights=PLAN_WEIGHTS, k=1)[0]
    is_trial    = random.random() < 0.22

    trial_start = signup if is_trial else None
    trial_end   = (signup + timedelta(days=30)) if is_trial else None

    # Churn logic: ~22% annual churn, higher for basic plan, lower for premium
    base_churn_prob = {"basic": 0.28, "standard": 0.20, "premium": 0.14}[plan]
    days_active     = (SIM_END - signup).days
    churn_prob      = base_churn_prob * (1.2 if days_active < 90 else 1.0)

    churned      = random.random() < (churn_prob * days_active / 365)
    churn_date   = None
    churn_reason = None
    status       = "active"

    if churned:
        min_tenure = 30
        max_churn  = (SIM_END - signup).days
        if max_churn > min_tenure:
            churn_offset = int(np.random.exponential(scale=max_churn * 0.4))
            churn_offset = max(min_tenure, min(churn_offset, max_churn))
            churn_date   = (signup + timedelta(days=churn_offset)).isoformat()
            churn_reason = random.choices(
                CHURN_REASONS,
                weights=[0.28, 0.22, 0.18, 0.15, 0.10, 0.07], k=1)[0]
            status = "churned"

    # ~4% paused
    if not churned and random.random() < 0.04:
        status = "paused"

    subscriber_rows.append({
        "subscriber_id":       sid,
        "email":               fake.unique.email(),
        "country":             country,
        "signup_date":         signup.isoformat(),
        "acquisition_channel": weighted_choice(ACQ_CHANNELS),
        "plan_type":           plan,
        "plan_price_usd":      PLAN_PRICES[plan],
        "subscription_status": status,
        "trial_start_date":    trial_start.isoformat() if trial_start else None,
        "trial_end_date":      trial_end.isoformat()   if trial_end   else None,
        "churn_date":          churn_date,
        "churn_reason":        churn_reason,
        "age_group":           random.choices(age_groups, weights=age_weights, k=1)[0],
        "device_preference":   random.choices(
                                   DEVICES,
                                   weights=[0.40,0.35,0.15,0.10], k=1)[0],
    })
    subscriber_ids.append(sid)

sub_df = pd.DataFrame(subscriber_rows)
sub_df.to_csv(f"{OUTPUT_DIR}/subscribers.csv", index=False)
print(f"  subscribers: {len(sub_df):,} rows")

# Build lookup maps for downstream tables
sub_info = {row["subscriber_id"]: row for row in subscriber_rows}

# ── 4. SUBSCRIPTIONS ──────────────────────────────────────────────────────────
print("Generating subscriptions...")

subscription_rows = []
subscription_map  = {}   # subscriber_id → list of subscription dicts

for sid, info in sub_info.items():
    signup     = date.fromisoformat(info["signup_date"])
    plan       = info["plan_type"]
    status     = info["subscription_status"]
    churn_date = date.fromisoformat(info["churn_date"]) if info["churn_date"] else None
    is_trial   = info["trial_start_date"] is not None

    # Trial subscription
    if is_trial:
        trial_end = date.fromisoformat(info["trial_end_date"])
        sub_id    = uid()
        subscription_rows.append({
            "subscription_id":     sub_id,
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

    # Main subscription
    billing_cycle = random.choices(["monthly", "annual"], weights=[0.72, 0.28], k=1)[0]
    price = PLAN_PRICES[plan]
    mrr   = price if billing_cycle == "monthly" else round(price * 12 * 0.85 / 12, 2)

    end_date = churn_date if churn_date else None
    sub_id   = uid()

    sub_record = {
        "subscription_id":     sub_id,
        "subscriber_id":       sid,
        "plan_type":           plan,
        "plan_price_usd":      price if billing_cycle == "monthly" else round(price * 12 * 0.85, 2),
        "billing_cycle":       billing_cycle,
        "status":              "cancelled" if churn_date else status,
        "start_date":          start.isoformat(),
        "end_date":            end_date.isoformat() if end_date else None,
        "mrr_usd":             mrr,
        "is_trial":            False,
        "payment_method":      random.choice(PAYMENT_METHODS),
        "cancellation_reason": info["churn_reason"] if churn_date else None,
    }
    subscription_rows.append(sub_record)
    subscription_map.setdefault(sid, []).append(sub_record)

subscriptions_df = pd.DataFrame(subscription_rows)
subscriptions_df.to_csv(f"{OUTPUT_DIR}/subscriptions.csv", index=False)
print(f"  subscriptions: {len(subscriptions_df):,} rows")

# ── 5. SUBSCRIPTION PLAN HISTORY ──────────────────────────────────────────────
print("Generating subscription_plan_history...")

plan_history_rows = []

# ~28% of active/paused subscribers changed plans at least once
eligible = [sid for sid, info in sub_info.items()
            if info["subscription_status"] in ("active", "paused")]

changers = random.sample(eligible, int(len(eligible) * 0.28))

for sid in changers:
    info    = sub_info[sid]
    signup  = date.fromisoformat(info["signup_date"])
    churn_d = date.fromisoformat(info["churn_date"]) if info["churn_date"] else SIM_END
    current = info["plan_type"]

    n_changes = random.choices([1, 2, 3], weights=[0.65, 0.25, 0.10], k=1)[0]

    # Reconstruct a plausible plan sequence ending at current plan.
    # Each step walks one level up or down in PLAN_ORDER so change_type
    # is always directionally consistent with old_plan vs new_plan.
    plan_sequence = [current]

    for _ in range(n_changes):
        lo = signup + timedelta(days=60)
        hi = min(churn_d, SIM_END) - timedelta(days=30)
        if lo >= hi:
            continue

        change_date = rand_date(lo, hi)
        new_plan    = plan_sequence[-1]
        new_idx     = PLAN_ORDER[new_plan]

        # Pick a prior plan that is genuinely different from new_plan
        # and adjacent (one tier up or down) to keep history believable
        candidates = [p for p, idx in PLAN_ORDER.items()
                      if abs(idx - new_idx) == 1]
        if not candidates:
            continue
        old_plan  = random.choice(candidates)
        old_price = PLAN_PRICES[old_plan]
        new_price = PLAN_PRICES[new_plan]

        if new_price > old_price:
            ctype = "upgrade"
        elif new_price < old_price:
            ctype = "downgrade"
        else:
            ctype = "reactivation"

        change_reasons = {
            "upgrade":      ["feature_need", "better_quality", "family_sharing", "promotion"],
            "downgrade":    ["cost_saving", "less_usage", "price_increase", "financial"],
            "reactivation": ["returning_user", "new_content", "promotion", None],
        }

        plan_history_rows.append({
            "change_id":     uid(),
            "subscriber_id": sid,
            "old_plan":      old_plan,
            "new_plan":      new_plan,
            "old_mrr_usd":   old_price,
            "new_mrr_usd":   new_price,
            "change_type":   ctype,
            "change_date":   change_date.isoformat(),
            "change_reason": random.choice(change_reasons[ctype]),
        })
        plan_sequence.append(old_plan)

plan_history_df = pd.DataFrame(plan_history_rows)
plan_history_df.to_csv(f"{OUTPUT_DIR}/subscription_plan_history.csv", index=False)
print(f"  subscription_plan_history: {len(plan_history_df):,} rows")

# ── 6. PAYMENTS ───────────────────────────────────────────────────────────────
print("Generating payments (~75k rows, this may take a moment)...")

payment_rows = []

currency_map = {
    "US": ("USD", 1.0),    "IN": ("INR", 83.0),  "GB": ("GBP", 0.79),
    "DE": ("EUR", 0.92),   "BR": ("BRL", 5.0),   "CA": ("CAD", 1.36),
    "FR": ("EUR", 0.92),   "AU": ("AUD", 1.53),  "MX": ("MXN", 17.0),
    "JP": ("JPY", 149.0),  "KR": ("KRW", 1320),  "NG": ("NGN", 780),
    "ZA": ("ZAR", 18.5),   "AR": ("ARS", 350),   "SG": ("SGD", 1.34),
}

for sub in subscription_rows:
    if sub["is_trial"]:
        continue

    sid    = sub["subscriber_id"]
    sub_id = sub["subscription_id"]
    cycle  = sub["billing_cycle"]
    price  = sub["plan_price_usd"]
    start  = date.fromisoformat(sub["start_date"])
    end    = date.fromisoformat(sub["end_date"]) if sub["end_date"] else SIM_END
    method = sub["payment_method"]

    interval_days = 30 if cycle == "monthly" else 365
    pay_date      = start

    # In incremental mode, skip payments we've already written
    effective_start = INCREMENTAL_FROM if INCREMENTAL_MODE else date.min

    is_first = True
    while pay_date <= min(end, SIM_END):
        if pay_date >= effective_start:
            period_end = pay_date + timedelta(days=interval_days - 1)

            failed   = random.random() < 0.035
            refunded = (not failed) and random.random() < 0.008
            disputed = (not failed) and (not refunded) and random.random() < 0.003

            if failed:
                pay_status  = "failed"
                fail_reason = random.choice(FAILURE_REASONS)
                # Simulate one retry 3 days later (~60% succeed on retry)
                retry_date = pay_date + timedelta(days=3)
                if retry_date <= min(end, SIM_END):
                    retry_success = random.random() < 0.60
                    payment_rows.append({
                        "payment_id":           uid(),
                        "subscription_id":      sub_id,
                        "subscriber_id":        sid,
                        "payment_date":         retry_date.isoformat(),
                        "billing_period_start": pay_date.isoformat(),
                        "billing_period_end":   period_end.isoformat(),
                        "amount_usd":           price,
                        "currency":             currency_map.get(sub_info[sid]["country"], ("USD", 1.0))[0],
                        "amount_local":         round(price * currency_map.get(sub_info[sid]["country"], ("USD", 1.0))[1], 2),
                        "status":               "succeeded" if retry_success else "failed",
                        "failure_reason":       None if retry_success else random.choice(FAILURE_REASONS),
                        "payment_method":       method,
                        "stripe_charge_id":     f"ch_{uid().replace('-','')[:24]}",
                        "is_renewal":           not is_first,
                        "is_retry":             True,
                        "discount_applied":     False,
                        "discount_pct":         None,
                    })
            elif refunded:
                pay_status  = "refunded"
                fail_reason = None
            elif disputed:
                pay_status  = "disputed"
                fail_reason = None
            else:
                pay_status  = "succeeded"
                fail_reason = None

            discount     = is_first and random.random() < 0.18
            discount_pct = round(random.choice([0.10, 0.20, 0.30, 0.50]), 2) if discount else None
            final_amount = round(price * (1 - discount_pct), 2) if discount else price

            country     = sub_info[sid]["country"]
            curr, fx    = currency_map.get(country, ("USD", 1.0))

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

payments_df = pd.DataFrame(payment_rows)
save_csv(payments_df, "payments", incremental=INCREMENTAL_MODE)

# ── 7. STREAM SESSIONS ────────────────────────────────────────────────────────
print("Generating stream_sessions (~500k rows, this takes a while)...")

content_weights = [content_popularity[cid] for cid in content_ids]

session_rows       = []
session_ids_by_sub = defaultdict(list)

active_subs = [
    sid for sid, info in sub_info.items()
    if info["subscription_status"] in ("active", "paused", "churned")
]

TARGET_SESSIONS    = 500_000
effective_start_dt = INCREMENTAL_FROM if INCREMENTAL_MODE else SIM_START

for sid in active_subs:
    info       = sub_info[sid]
    signup     = date.fromisoformat(info["signup_date"])
    churn_d    = date.fromisoformat(info["churn_date"]) if info["churn_date"] else SIM_END
    active_end = min(churn_d, SIM_END)

    tenure_days = max((active_end - signup).days, 1)

    engagement_level = random.choices(
        ["heavy", "medium", "light", "very_light"],
        weights=[0.15, 0.40, 0.30, 0.15], k=1)[0]
    monthly_rate = {"heavy": 45, "medium": 18, "light": 6, "very_light": 2}[engagement_level]
    # Cap per-subscriber sessions so the total stays near TARGET_SESSIONS.
    # Allow up to 4x the average to preserve the heavy-user shape.
    max_per_sub = max(1, int(TARGET_SESSIONS / len(active_subs) * 4))
    n_sessions  = min(max_per_sub, max(1, int(np.random.poisson(monthly_rate * tenure_days / 30))))

    country = info["country"]
    device  = info["device_preference"]

    for _ in range(n_sessions):
        session_date = rand_date(signup, active_end)

        # In incremental mode, skip sessions before the cutoff
        if INCREMENTAL_MODE and session_date < INCREMENTAL_FROM:
            continue

        wm = weekend_multiplier(session_date)
        if random.random() > wm * 0.6:
            pass

        hour     = hour_of_day_weight()
        start_dt = datetime(
            session_date.year, session_date.month, session_date.day,
            hour, random.randint(0, 59), random.randint(0, 59)
        )

        cid     = random.choices(content_ids, weights=content_weights, k=1)[0]
        # O(1) dict lookup instead of O(n) .loc search
        runtime = content_runtime[cid]

        completion_base = {"heavy": 0.78, "medium": 0.58, "light": 0.42, "very_light": 0.30}[engagement_level]
        completion_pct  = min(1.0, max(0.01, np.random.beta(
            completion_base * 5, (1 - completion_base) * 5
        )))
        watched_mins = round(runtime * completion_pct, 1)
        end_dt       = start_dt + timedelta(minutes=watched_mins)

        dev    = device if random.random() < 0.75 else random.choice(DEVICES)
        plan   = info["plan_type"]
        qual_w = {"basic": [0.6, 0.35, 0.05],
                  "standard": [0.15, 0.65, 0.20],
                  "premium":  [0.05, 0.35, 0.60]}[plan]
        quality = random.choices(QUALITY_LEVELS, weights=qual_w, k=1)[0]

        buffer_base = {"SD": 0.1, "HD": 0.25, "4K": 0.45}[quality]
        buf_events  = np.random.poisson(buffer_base * (watched_mins / 10))

        sess_id = uid()
        session_rows.append({
            "session_id":          sess_id,
            "subscriber_id":       sid,
            "content_id":          cid,
            "session_start":       start_dt.isoformat(),
            "session_end":         end_dt.isoformat(),
            "duration_minutes":    watched_mins,
            "content_runtime_min": runtime,
            "completion_pct":      round(completion_pct, 4),
            "device_type":         dev,
            "country":             country,
            "quality_streamed":    quality,
            "buffering_events":    int(buf_events),
            "was_resumed":         random.random() < 0.18,
            "referral_source":     random.choices(
                                       ["home_page","search","recommendation",
                                        "continue_watching","external"],
                                       weights=[0.25,0.15,0.35,0.20,0.05], k=1)[0],
        })
        session_ids_by_sub[sid].append(sess_id)

sessions_df = pd.DataFrame(session_rows)
save_csv(sessions_df, "stream_sessions", incremental=INCREMENTAL_MODE)

# ── 8. RECOMMENDATION EVENTS ──────────────────────────────────────────────────
print("Generating recommendation_events (~200k rows)...")

rec_rows      = []
TARGET_RECS   = 200_000

def algo_for_date(d):
    if d < date(2023, 7, 1):  return "v1"
    if d < date(2024, 4, 1):  return "v2"
    return "v3"

ctr_by_algo = {"v1": 0.08, "v2": 0.12, "v3": 0.17}

subs_for_recs = random.choices(list(session_ids_by_sub.keys()),
                                k=min(TARGET_RECS, len(session_ids_by_sub) * 15))

for sid in subs_for_recs:
    if len(rec_rows) >= TARGET_RECS:
        break

    info     = sub_info[sid]
    signup   = date.fromisoformat(info["signup_date"])
    churn_d  = date.fromisoformat(info["churn_date"]) if info["churn_date"] else SIM_END
    rec_date = rand_date(signup, min(churn_d, SIM_END))

    if INCREMENTAL_MODE and rec_date < INCREMENTAL_FROM:
        continue

    algo     = algo_for_date(rec_date)
    ctr      = ctr_by_algo[algo]
    rec_type = random.choice(REC_TYPES)

    n_shown      = random.randint(5, 15)
    shown_content = random.choices(content_ids, weights=content_weights, k=n_shown)

    for pos, cid in enumerate(shown_content, 1):
        # Position decay on CTR
        was_clicked = random.random() < (ctr / pos ** 0.4)

        was_streamed_this = False
        sess_link         = None

        if was_clicked:
            was_streamed_this = random.random() < 0.52
            if was_streamed_this:
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
            "was_streamed":        was_streamed_this,
            "session_id":          sess_link,
            "algorithm_version":   algo,
        })

        if len(rec_rows) >= TARGET_RECS:
            break

recs_df = pd.DataFrame(rec_rows[:TARGET_RECS])
save_csv(recs_df, "recommendation_events", incremental=INCREMENTAL_MODE)

# ── 9. SEARCH EVENTS ──────────────────────────────────────────────────────────
print("Generating search_events (~100k rows)...")

search_rows     = []
TARGET_SEARCHES = 100_000

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

for _ in range(TARGET_SEARCHES):
    sid    = random.choice(list(sub_info.keys()))
    info   = sub_info[sid]
    signup = date.fromisoformat(info["signup_date"])
    churn_d= date.fromisoformat(info["churn_date"]) if info["churn_date"] else SIM_END
    s_date = rand_date(signup, min(churn_d, SIM_END))

    if INCREMENTAL_MODE and s_date < INCREMENTAL_FROM:
        continue

    stype   = random.choices(list(query_templates.keys()),
                              weights=[0.30,0.25,0.15,0.20,0.10], k=1)[0]
    query   = query_templates[stype]()
    results = random.randint(0, 50)
    clicked = results > 0 and random.random() < 0.62
    clicked_pos = random.randint(1, min(results, 10)) if clicked else None
    cid_clicked = random.choice(content_ids) if clicked else None
    sess_started = clicked and random.random() < 0.48

    search_rows.append({
        "search_id":          uid(),
        "subscriber_id":      sid,
        "search_timestamp":   datetime(s_date.year, s_date.month, s_date.day,
                                       hour_of_day_weight(), random.randint(0,59)).isoformat(),
        "query_text":         query,
        "query_type":         stype,
        "results_returned":   results,
        "clicked_position":   clicked_pos,
        "content_id_clicked": cid_clicked,
        "session_started":    sess_started,
        "device_type":        random.choice(DEVICES),
    })

search_df = pd.DataFrame(search_rows)
save_csv(search_df, "search_events", incremental=INCREMENTAL_MODE)

# ── 10. USER WATCHLISTS ───────────────────────────────────────────────────────
print("Generating user_watchlists (~50k rows)...")

watchlist_rows = []
TARGET_WL      = 50_000

sources = ["browse", "recommendation", "search", "share", "trailer"]

for _ in range(TARGET_WL):
    sid    = random.choice(list(sub_info.keys()))
    info   = sub_info[sid]
    signup = date.fromisoformat(info["signup_date"])
    churn_d= date.fromisoformat(info["churn_date"]) if info["churn_date"] else SIM_END
    add_d  = rand_date(signup, min(churn_d, SIM_END))

    if INCREMENTAL_MODE and add_d < INCREMENTAL_FROM:
        continue

    cid       = random.choices(content_ids, weights=content_weights, k=1)[0]
    removed   = random.random() < 0.35
    remove_ts = None
    if removed:
        remove_lo = add_d + timedelta(days=1)
        remove_hi = min(churn_d, SIM_END)
        # Only attempt removal if there's at least one day of gap
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
                                      random.randint(0,23), random.randint(0,59)).isoformat(),
        "removed_timestamp": remove_ts,
        "was_streamed":      was_streamed,
        "stream_session_id": linked_sess,
        "source":            random.choices(
                                 sources, weights=[0.30,0.28,0.20,0.12,0.10], k=1)[0],
    })

watchlist_df = pd.DataFrame(watchlist_rows)
save_csv(watchlist_df, "user_watchlists", incremental=INCREMENTAL_MODE)

# ── SUMMARY ───────────────────────────────────────────────────────────────────
mode_label = f"INCREMENTAL (from {INCREMENTAL_FROM})" if INCREMENTAL_MODE else "FULL REGENERATION"
print("\n" + "="*60)
print(f"  DATA GENERATION COMPLETE  [{mode_label}]")
print("="*60)

tables = [
    ("content_catalog",           content_df),
    ("content_genre_bridge",      bridge_df),
    ("subscribers",               sub_df),
    ("subscriptions",             subscriptions_df),
    ("subscription_plan_history", plan_history_df),
    ("payments",                  payments_df),
    ("stream_sessions",           sessions_df),
    ("recommendation_events",     recs_df),
    ("search_events",             search_df),
    ("user_watchlists",           watchlist_df),
]

total = 0
for name, df in tables:
    path    = f"{OUTPUT_DIR}/{name}.csv"
    size_mb = os.path.getsize(path) / 1024 / 1024 if os.path.exists(path) else 0
    print(f"  {name:<32} {len(df):>8,} rows   {size_mb:>6.1f} MB")
    total  += len(df)

print(f"\n  {'TOTAL':<32} {total:>8,} rows")
print(f"  Date range: {SIM_START} → {SIM_END}")
print(f"  Output dir: ./{OUTPUT_DIR}/")
print("="*60)