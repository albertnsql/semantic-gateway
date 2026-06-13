# Offline Evals

This directory contains the offline accuracy test harness for the `IntentExtractor`. It uses a pinned golden dataset of questions and expected answers to ensure the LLM's natural language understanding does not regress when prompts or models are updated.

## Contents

- `golden_set.json` — 20 pinned Q&A cases across 7 categories (metric extraction, dimension extraction, filters, time ranges, etc.)
- `run_evals.py` — The evaluation runner script.
- `snapshots/` — Directory where dated eval runs are saved.

## Usage

Run the eval harness from the project root:

```bash
# Run all cases and write a dated snapshot
python backend/evals/run_evals.py --snapshot

# Gate a CI merge — fail if accuracy drops below 85%
python backend/evals/run_evals.py --fail-under 85

# Run only a specific category to debug failures
python backend/evals/run_evals.py --category hallucination_resistance --verbose
```
