# Financial correctness Phase 1 implementation plan

1. Add strict input validation and regression tests for dates, periods, limits, numeric ranges, and enum-like parameters.
2. Add idempotent schema migrations for account opening balance, reminder schedule/currency data, reminder-marker currencies, and normalized nullable budget keys.
3. Replace direct live-cache synchronization with validated staging and atomic replacement; make full sync a real snapshot replacement.
4. Correct net-worth and liquidity semantics while returning explicit alternative totals rather than hiding assumptions.
5. Correct budget calculations for custom month boundaries, JSON tags, planned-marker currency, zero-budget spending, and categories without budget rows.
6. Reconcile debt history to authoritative account balances and expose attribution gaps.
7. Correct account-flow currency labeling and include transfers in signed movement.
8. Enforce strict search bounds and FX validity; return raw and converted transaction amounts.
9. Route the package command through a hardened entrypoint while preserving upstream modules for mergeability.
10. Add CI, operational documentation, and run full local verification before publishing a draft PR.
