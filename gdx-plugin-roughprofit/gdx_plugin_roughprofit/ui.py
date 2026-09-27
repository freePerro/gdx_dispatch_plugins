"""Declarative UI (host-rendered, no plugin JS — ADR-013).

The host serves this dict verbatim to every caller, so screens cannot vary by
who is looking: a view-only user sees the Add forms too, and the plugin
refuses the write with a message saying why. `category: "accounting"` puts
the entry beside the books screens; an older host without that key falls back
to the Plugins group.
"""
from gdx_plugin_roughprofit.compute import CATEGORY_LABELS

_BASE = "/api/plugins/roughprofit"

_CATEGORY_OPTIONS = [{"label": label, "value": key} for key, label in CATEGORY_LABELS.items()]
_REMOVE_RULE = {"label": "Remove this rule", "value": "remove"}

UI = {
    "icon": "pi pi-chart-bar",
    "category": "accounting",
    "screens": [
        {
            "type": "list",
            "title": "Monthly",
            "endpoint": f"{_BASE}/monthly",
            "columns": [
                # Headline figures first: 15 columns overflow a desktop
                # table, and whatever sits right of the fold is behind a
                # sideways scroll. The supporting detail goes there.
                {"field": "month", "label": "Month"},
                {"field": "deposited", "label": "Deposited"},
                {"field": "billed", "label": "Billed"},
                {"field": "costs", "label": "Costs"},
                {"field": "profit_deposits", "label": "Profit on deposits"},
                {"field": "profit_billed", "label": "Profit on billed"},
                {"field": "true_profit", "label": "True profit"},
                {"field": "owner_pay", "label": "Owner pay taken"},
                {"field": "left_in_business", "label": "Left in the business"},
                {"field": "unsorted", "label": "Unsorted $"},
                {"field": "flags", "label": "Flags"},
                {"field": "still_unpaid", "label": "Still unpaid (today)"},
                {"field": "supplier_delta", "label": "Supplier owed Δ"},
                {"field": "owner_labor", "label": "Owner labor"},
                {"field": "loans_equipment", "label": "Loans + equipment"},
            ],
        },
        {
            "type": "list",
            "title": "Unsorted",
            "endpoint": f"{_BASE}/unsorted",
            "columns": [
                {"field": "description", "label": "Payee / description"},
                {"field": "memo", "label": "Memo"},
                {"field": "direction", "label": "Direction"},
                {"field": "total", "label": "Total"},
                {"field": "count", "label": "Lines"},
                {"field": "last_seen", "label": "Last seen"},
                {"field": "note", "label": "Note"},
            ],
        },
        {
            "type": "list",
            "title": "Rules",
            "endpoint": f"{_BASE}/rules",
            "columns": [
                {"field": "match_text", "label": "Match text"},
                {"field": "look_in", "label": "Look in"},
                {"field": "category", "label": "Category"},
                {"field": "lines", "label": "Lines sorted"},
                {"field": "changed_by", "label": "Changed by"},
                {"field": "changed_at", "label": "Changed"},
            ],
            "create": {
                "endpoint": f"{_BASE}/rules",
                "fields": [
                    {"name": "match_text", "label": "Match text", "required": True},
                    {
                        "name": "look_in", "label": "Look in", "type": "select", "default": "either",
                        "options": [
                            {"label": "Payee or memo", "value": "either"},
                            {"label": "Payee", "value": "payee"},
                            {"label": "Memo", "value": "memo"},
                        ],
                    },
                    {
                        "name": "category", "label": "Category", "type": "select",
                        "options": _CATEGORY_OPTIONS + [_REMOVE_RULE],
                    },
                ],
            },
        },
        {
            "type": "list",
            "title": "Owner labor",
            "endpoint": f"{_BASE}/owner-labor",
            "columns": [
                {"field": "effective_month", "label": "From month"},
                {"field": "amount", "label": "Per month"},
                {"field": "set_by", "label": "Set by"},
                {"field": "set_at", "label": "Set"},
            ],
            "create": {
                "endpoint": f"{_BASE}/owner-labor",
                "fields": [
                    {"name": "effective_month", "label": "From month (YYYY-MM)", "required": True},
                    {"name": "amount", "label": "Dollars per month", "type": "number"},
                ],
            },
        },
        {
            "type": "list",
            "title": "Who can see it",
            "endpoint": f"{_BASE}/access",
            "columns": [
                {"field": "who", "label": "Who"},
                {"field": "level", "label": "Access"},
                {"field": "granted_by", "label": "Granted by"},
                {"field": "granted_at", "label": "Granted"},
            ],
            "create": {
                "endpoint": f"{_BASE}/access",
                "fields": [
                    {
                        "name": "subject", "label": "Grant to", "type": "select", "filter": True,
                        "options_endpoint": f"{_BASE}/access/subjects",
                    },
                    {
                        "name": "level", "label": "Level", "type": "select",
                        "options": [
                            {"label": "View", "value": "view"},
                            {"label": "View + edit", "value": "edit"},
                            {"label": "Remove access", "value": "remove"},
                        ],
                    },
                ],
            },
        },
        {
            "type": "help",
            "title": "Help",
            "sections": [
                {
                    "heading": "What this is",
                    "body": [
                        "A rough answer to \"do we look profitable?\", one row per month.",
                        "It reads the bank feed, invoices and supplier statements. It changes",
                        "nothing in the books, QuickBooks or tax. It is a rough view, not the books.",
                        "Months start with the first bank data, up to the last 12. A month is",
                        "flagged when a connected account's data starts after the 1st, or when",
                        "its feed's data does not reach the month's end (a feed that stopped",
                        "syncing, or a first download that has not finished), because that",
                        "account's money is missing from it. An account with no transactions",
                        "at all counts as covered once it has been connected a week. A feed that",
                        "lapsed and came back without refilling the gap is not detected; the",
                        "bank feed normally refills it. An account that should not count at all",
                        "can be switched off in Bank Feeds.",
                        "A \"Bank feed unconfirmed\" note at the top means the bank feed has not",
                        "recorded how far some accounts' transactions reach (Bank Feeds shows the",
                        "sync status). For those accounts, if transactions stopped arriving while",
                        "the balance kept updating, their money is missing and no month is flagged.",
                    ],
                },
                {
                    "heading": "The columns",
                    "body": [
                        "- Deposited: money in sorted as Revenue, plus unsorted money in.",
                        "- Billed: invoices dated that month, less credit memos issued that month.",
                        "- Still unpaid (today): what that month's invoices still owe as of today.",
                        "- Costs: every cost category, plus unsorted money out.",
                        "- Supplier owed Δ: change in what supplier statements say we owe. Blank "
                        "when a supplier's statement is missing or over 35 days old.",
                        "- Profit on deposits: Deposited − Costs.",
                        "- Profit on billed: Billed − Costs − Supplier owed Δ.",
                        "- True profit: Profit on deposits − the owner labor value.",
                        "- Owner pay taken, Loans + equipment: below profit, not costs.",
                        "- Left in the business: Profit on deposits − owner pay − loans − equipment.",
                        "- Unsorted $: how much of the month is a guess. Keep it small.",
                    ],
                },
                {
                    "heading": "Why owner pay is below profit",
                    "body": [
                        "Owner draws are the owner's wages, so they are not a cost of running the",
                        "business. They are shown, not hidden: Owner pay taken sits below profit,",
                        "and True profit subtracts what the owner's labor is worth (Owner labor tab).",
                    ],
                },
                {
                    "heading": "Sorting the bank lines",
                    "body": [
                        "Moves between our own connected accounts drop out on their own when the",
                        "amounts match within a day and one side says transfer.",
                        "Everything else is sorted by Rules: text found in the payee or memo picks",
                        "a category. The longest matching text wins. Rules apply instantly to",
                        "every month. To change a rule, add it again with the same match text and",
                        "a new category; choose \"Remove this rule\" to delete it.",
                        "Unsorted lists what no rule covers, largest first. \"possible transfer\"",
                        "means an equal and opposite line exists on another account; if it really",
                        "is one, write a Transfer rule.",
                    ],
                },
                {
                    "heading": "Approximations",
                    "body": [
                        "- Credit card payment counts as a cost, because the card's own purchases "
                        "are not visible unless the card account is in the bank feed.",
                        "- If the card account IS in the bank feed, sort its payment lines (on both "
                        "sides) as Transfer instead, or the payment counts twice. Unsorted marks "
                        "them \"possible transfer\".",
                        "- Loan payments are not split into interest and principal.",
                        "- \"billing looks incomplete\" means no invoices that month, or billing under "
                        "half of deposits. It is a hint, not a finding.",
                    ],
                },
                {
                    "heading": "Who can see it",
                    "body": [
                        "The owner always can. Anyone else needs a grant from the owner on the",
                        "\"Who can see it\" tab, by role or by person. View shows the numbers;",
                        "View + edit also changes rules and owner labor. Only the owner changes",
                        "who has access.",
                        "A grant here is one of two locks. The app's own gate comes first: a role",
                        "must also have \"Use Rough Profit\" (and \"Change data in Rough Profit\" for",
                        "View + edit) under Roles & Permissions. Admins already have it; office",
                        "and technician roles do not.",
                    ],
                },
            ],
        },
    ],
}
