# gdx-plugin-roughprofit — "do we look profitable?"

**Status:** PLAN — code complete on branch `feat/roughprofit`; not merged,
not released, not installed anywhere (2026-09-26). Needs core ≥ 1.125.0,
the release it was built and walked against. Design record: core's
`docs/design/rough-profit-plugin-plan.md`.

One row per calendar month, from the bank feed, the business's own invoices
and supplier statements. Bank lines are sorted by payee rules the owner
writes; nothing is stored per transaction, so changing a rule re-sorts every
month at once. It is a rough view, not the books: it writes to no core table,
and nothing it decides reaches the GL, QuickBooks or tax.

## Screens

| Tab | What it is |
| --- | --- |
| Monthly | Deposited, billed, costs, profit on deposits, profit on billed, true profit (after owner labor), owner pay taken, left in the business, how much is unsorted, and heuristic flags. |
| Unsorted | Bank lines no rule covers, grouped and largest first — the to-do list for writing rules. Marks "possible transfer" where an equal and opposite line sits on another account. |
| Rules | Match text in payee / memo / either → a category. One form adds, changes (same text, new category) or removes a rule. |
| Owner labor | What the owner's labor is worth per month, effective from a month. Append-only. |
| Who can see it | Owner-only access list: grant View or View + edit to a role or a person. |
| Help | What each column means and what is approximate. |

## What it reads (read-only, raw `text()` SQL)

`bank_feed_accounts`, `bank_feed_transactions`, `invoices`,
`invoice_adjustments` (credit memos only), `vendor_statements`,
`vendor_statement_lines`, `users` (names and roles for the access list).
If core's ADR-013 scoped plugin DB role is ever built, it must grant this
plugin `SELECT` on exactly these tables.

Its own tables: `plug_roughprofit_rules`, `_rule_changes`, `_owner_labor`,
`_access`, `_access_changes`. Every change to a rule, the owner-labor value or
the access list is recorded with who, what and when (core's
`log_audit_event` is not part of `plugin_api`, so the plugin keeps its own
trail).

## Who can use it — two locks

1. **Core's proxy** lets a request through on the role's "Use Rough Profit" /
   "Change data in Rough Profit" permission, or on the blanket
   `plugins.read` / `plugins.write`, which admins hold.
2. **The plugin's own gate** then refuses everyone except the owner role
   (always, hard-coded) and whoever the owner grants on "Who can see it".

So admins pass lock 1 but see nothing until granted. Office and technician
roles need both: the grant here and the permission under Roles & Permissions.

## Known limits

- The host renders a refused form submit as an error page (core
  `PluginScreen.onCreate` does not catch). The plugin accepts loose input —
  months as `2026-09`, `9/2026` or `Sep 2026`, cleared selects fall back to a
  default — and every refusal is one plain sentence, but a refusal still
  shows that page.
- The screens are the same for every caller (the host serves `/ui`
  statically), so a view-only user sees the Add forms and is refused on
  submit.
- Month flags can only be as good as core's bank-feed sync record. Where core
  records no sync progress, Monthly opens with a note that the feed has not
  confirmed its accounts are complete; a card whose transactions stopped while
  its balance kept updating is not flagged month by month there.
- The Monthly table has 15 columns; the headline figures come first and the
  supporting detail scrolls sideways on a desktop.

## Tests

`tests/test_roughprofit.py` runs in the `contract` workflow on SQLite and on a
Postgres service, with core's schema built from its ORM metadata. Locally:

```bash
PYTHONPATH="$CORE:$PWD/gdx-plugin-roughprofit" \
  ROUGHPROFIT_TEST_PG_URL=postgresql://…/throwaway \
  python -m pytest gdx-plugin-roughprofit/tests
```

`ROUGHPROFIT_TEST_PG_URL` must point at a throwaway database: the fixture
drops and recreates its `public` schema.
