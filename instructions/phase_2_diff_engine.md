# Phase 2: Deterministic Matching & 3-Way Differential Engine

## 1. Overview
Phase 2 implements the core deterministic business logic for contact synchronization. It performs:
1. Multi-field weighted contact identity matching.
2. 3-way differential merge evaluation ($S_{\text{base}}$ vs $S_{\text{goog}}$ vs $S_{\text{ms}}$).
3. Field-level conflict isolation and safe non-destructive merges.
4. Deletion safety policy enforcement (`ORPHANED_IN_PROVIDER`).
5. Execution plan generation (`SyncPlan`).

---

## 2. Core Modules

### 2.1. Contact Matcher (`execution/matcher.py`)
- Weighted confidence scoring table:
  - **Stored Mapping ID Match:** $100$
  - **Primary / Normalized Email Match:** $90$
  - **Normalized E.164 Phone Match:** $70$
  - **Full Name + Company Match:** $60$
  - **Full Name Only Match:** $30$
- Output categorization:
  - `DEFINITE_MATCH` ($\ge 80$): Automatic pairing.
  - `AMBIGUOUS` ($50 - 79$): Potential duplicate held for user confirmation.
  - `UNIQUE` ($< 50$): Unmatched separate contact.

### 2.2. Three-Way Diff Engine & Sync Planner (`execution/diff_engine.py`)
- Evaluates field states across:
  - $S_{\text{base}}$: Last known synchronized snapshot.
  - $S_{\text{goog}}$: Current Google Contact state.
  - $S_{\text{ms}}$: Current Microsoft Contact state.
- Classifies changes:
  - **Clean Additions:** New contact on one side $\rightarrow$ Plan creation on the opposite provider.
  - **Safe Field Updates:** Unilateral modifications or non-overlapping field additions $\rightarrow$ Plan field patch propagation.
  - **Hard Conflicts:** Bilateral modifications with divergent values $\rightarrow$ Isolate record and construct `ConflictItem` with choices (`keep_both`, `use_google`, `use_microsoft`, `skip`).
  - **Deletion Safety:** Deletions without explicit opt-in (`allow_deletions=False`) are flagged as `ORPHANED_IN_PROVIDER` and preserved.
  - **No-Op:** Identical or unchanged states recorded without generating write operations.

---

## 3. Verification & Test Suite
- Automated test suite in `tests/test_diff_engine.py` covering:
  - Weighted matching scoring & categorization.
  - Clean one-sided additions (Google $\rightarrow$ MS and MS $\rightarrow$ Google).
  - Safe field merges and non-destructive additions.
  - Hard conflict isolation on diverging phone numbers/emails.
  - Identical no-op record detection.
  - Deletion safety flag verification.
