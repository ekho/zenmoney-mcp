from __future__ import annotations

import json
from datetime import date

import pytest

from zenmoney_mcp.financial_correctness import (
    FinancialDataError,
    analyze_spending,
    check_budget_health,
    convert_currency,
    get_account_flow,
    get_debts,
    get_exchange_rates,
    get_liquidity,
    get_net_worth,
    search_transactions,
)
from zenmoney_mcp.hardened_database import CurrencyRateError, HardenedDatabase
from zenmoney_mcp.validation import InputValidationError, resolve_date_range


def make_db() -> HardenedDatabase:
    db = HardenedDatabase(':memory:')
    db.init_schema()
    conn = db.connect()
    conn.executemany(
        'INSERT INTO instruments(id,title,short_title,symbol,rate,changed) VALUES (?,?,?,?,?,1)',
        [(1,'Ruble','RUB','₽',1.0),(2,'Dollar','USD','$',90.0)],
    )
    conn.execute('INSERT INTO users(id,login,currency,parent,month_start_day,changed) VALUES (1,"u",1,NULL,1,1)')
    conn.commit()
    return db


def add_account(db, *, id, title, type, balance, instrument=1, credit_limit=0, in_balance=1, savings=0):
    db.connect().execute(
        '''INSERT INTO accounts(id,title,type,instrument,balance,credit_limit,in_balance,savings,archive,user,changed)
           VALUES (?,?,?,?,?,?,?,?,0,1,1)''',
        (id,title,type,instrument,balance,credit_limit,in_balance,savings),
    )
    db.connect().commit()


def add_tx(db, *, id, amount, tx_type='outcome', account='cash', instrument=1, tx_date=None, tag=None,
           other_account=None, other_instrument=None, other_amount=None, payee='Shop', hold=0):
    tx_date = tx_date or date.today().isoformat()
    income = amount if tx_type == 'income' else (other_amount or 0 if tx_type == 'transfer_out' else amount if tx_type == 'transfer_in' else 0)
    outcome = amount if tx_type == 'outcome' else (amount if tx_type == 'transfer_out' else other_amount or 0 if tx_type == 'transfer_in' else 0)
    if tx_type == 'income':
        income_account, outcome_account = account, account
        income_instrument, outcome_instrument = instrument, instrument
    elif tx_type == 'outcome':
        income_account, outcome_account = account, account
        income_instrument, outcome_instrument = instrument, instrument
    elif tx_type == 'transfer_out':
        income_account, outcome_account = other_account, account
        income_instrument, outcome_instrument = other_instrument, instrument
    else:
        income_account, outcome_account = account, other_account
        income_instrument, outcome_instrument = instrument, other_instrument
    db.connect().execute(
        '''INSERT INTO transactions(id,date,user,deleted,hold,income,income_instrument,income_account,
           outcome,outcome_instrument,outcome_account,tag,payee,changed)
           VALUES (?,?,1,0,?,?,?,?,?,?,?,?,?,1)''',
        (id,tx_date,hold,income,income_instrument,income_account,outcome,outcome_instrument,outcome_account,
         json.dumps([tag]) if tag else None,payee),
    )
    db.connect().commit()


def test_net_worth_excludes_out_of_balance_from_primary_total():
    db=make_db()
    add_account(db,id='cash',title='Cash',type='checking',balance=1000)
    add_account(db,id='save',title='Save',type='deposit',balance=5000)
    add_account(db,id='excluded',title='Excluded',type='debt',balance=300,in_balance=0)
    result=get_net_worth(db)
    assert result['net_worth'] == 6000
    assert result['net_worth_all_accounts'] == 6300
    assert result['out_of_balance']['total'] == 300


def test_liquidity_separates_credit_accessible_savings_and_deposits():
    db=make_db()
    add_account(db,id='cash',title='Cash',type='checking',balance=500)
    add_account(db,id='card',title='Card',type='ccard',balance=-200,credit_limit=1000)
    add_account(db,id='save',title='Save',type='checking',balance=800,savings=1)
    add_account(db,id='deposit',title='Deposit',type='deposit',balance=2000)
    result=get_liquidity(db)
    assert result['liquid_own'] == 500
    assert result['credit_available'] == 800
    assert result['savings_accessible'] == 800
    assert result['restricted_savings'] == 2000
    assert result['total_available'] == 1300
    assert result['total_spendable_with_credit'] == 2100


def test_budget_uses_json_marker_tags_and_marks_zero_budget_spending():
    db=make_db()
    add_account(db,id='cash',title='Cash',type='checking',balance=1000)
    db.connect().executemany(
        'INSERT INTO tags(id,title,parent,show_outcome,budget_outcome,user,changed) VALUES (?,?,NULL,1,1,1,1)',
        [('food','Food'),('extra','Extra')],
    )
    today=date.today(); month=today.strftime('%Y-%m-01')
    db.connect().executemany(
        'INSERT INTO budgets(user,tag,date,income,income_lock,outcome,outcome_lock,changed,tag_key) VALUES (1,?,?,0,0,?,0,1,?)',
        [('food',month,1000,'food'),('extra',month,0,'extra')],
    )
    db.connect().execute(
        '''INSERT INTO reminder_markers(id,user,date,state,income,outcome,outcome_account,tag,changed)
           VALUES ('r1',1,?,'planned',0,200,'cash',?,1)''',
        (today.isoformat(),json.dumps(['food'])),
    )
    add_tx(db,id='food-actual',amount=1200,tag='food')
    add_tx(db,id='extra-actual',amount=300,tag='extra')
    result=check_budget_health(db, month=today.strftime('%Y-%m'))
    food=next(x for x in result['categories'] if x['tag_id']=='food')
    extra=next(x for x in result['categories'] if x['tag_id']=='extra')
    assert food['planned'] == 1200
    assert food['actual'] == 1200
    assert extra['status'] == 'unbudgeted_spending'
    assert extra['pct_used'] is None


def test_debts_use_account_balance_and_report_reconciliation_gap():
    db=make_db()
    add_account(db,id='debt',title='Debt',type='debt',balance=500)
    add_account(db,id='cash',title='Cash',type='checking',balance=1000)
    add_tx(db,id='lend',amount=300,tx_type='transfer_in',account='debt',instrument=1,
           other_account='cash',other_instrument=1,other_amount=300,payee='Alice')
    result=get_debts(db)
    assert result['summary']['total_owed_to_you'] == 500
    alice=next(x for x in result['by_counterparty'] if x['counterparty']=='Alice')
    unallocated=next(x for x in result['by_counterparty'] if x['counterparty']=='Unallocated balance')
    assert alice['net_amount'] == 300
    assert unallocated['net_amount'] == 200
    assert result['accounts'][0]['reconciliation_gap'] == 200


def test_account_flow_includes_transfers_and_returns_account_and_user_currency():
    db=make_db()
    add_account(db,id='usd',title='USD',type='checking',balance=100,instrument=2)
    add_account(db,id='rub',title='RUB',type='checking',balance=1000,instrument=1)
    add_tx(db,id='income',amount=10,tx_type='income',account='usd',instrument=2,payee='Employer')
    add_tx(db,id='expense',amount=2,tx_type='outcome',account='usd',instrument=2)
    add_tx(db,id='transfer',amount=3,tx_type='transfer_out',account='usd',instrument=2,
           other_account='rub',other_instrument=1,other_amount=270)
    result=get_account_flow(db,'usd','this_month')
    assert result['account']['currency'] == 'USD'
    assert result['account']['balance_converted'] == 9000
    assert result['summary']['net_change'] == 5
    assert result['summary']['net_change_converted'] == 450
    transfer=next(x for x in result['transactions'] if x['id']=='transfer')
    assert transfer['signed_change'] == -3
    assert transfer['amount_converted'] == 270


def test_spending_rejects_transfer_inclusion_and_routes_to_transfer_tool():
    db=make_db()
    with pytest.raises(InputValidationError, match='analyze_transfers'):
        analyze_spending(db, include_transfers=True)


def test_search_rejects_unbounded_limit_and_returns_converted_amount_at_maximum():
    db=make_db()
    add_account(db,id='usd',title='USD',type='checking',balance=100,instrument=2)
    for i in range(205):
        add_tx(db,id=f'tx-{i}',amount=1,tx_type='outcome',account='usd',instrument=2,payee='X')
    with pytest.raises(InputValidationError, match='limit must be between 1 and 200'):
        search_transactions(db,limit=999)
    with pytest.raises(InputValidationError, match='limit must be between 1 and 200'):
        search_transactions(db,limit=0)
    result=search_transactions(db,limit=200)
    assert result['limit_applied'] == 200
    assert result['returned_count'] == 200
    assert result['transactions'][0]['amount_converted'] == 90
    assert result['transactions'][0]['converted_currency'] == 'RUB'


def test_search_paginates_all_uncategorized_outcomes_by_amount_descending():
    db = make_db()
    add_account(db, id='cash', title='Cash', type='checking', balance=1000)
    db.connect().execute(
        "INSERT INTO tags(id,title,parent,show_outcome,user,changed) "
        "VALUES ('food','Food',NULL,1,1,1)"
    )
    for tx_id, amount, tag in (
        ('a', 100, None),
        ('b', 300, None),
        ('c', 200, None),
        ('d', 300, None),
        ('categorized', 999, 'food'),
    ):
        add_tx(
            db,
            id=tx_id,
            amount=amount,
            account='cash',
            tx_date='2026-01-15',
            tag=tag,
        )

    first = search_transactions(
        db,
        start_date='2026-01-01',
        end_date='2026-01-31',
        tx_type='outcome',
        category_state='uncategorized',
        sort_by='amount',
        sort_order='desc',
        limit=2,
    )
    second = search_transactions(
        db,
        start_date='2026-01-01',
        end_date='2026-01-31',
        tx_type='outcome',
        category_state='uncategorized',
        sort_by='amount',
        sort_order='desc',
        cursor=first['next_cursor'],
        limit=2,
    )

    transactions = first['transactions'] + second['transactions']
    assert [item['amount_converted'] for item in transactions] == [300, 300, 200, 100]
    assert [item['id'] for item in transactions[:2]] == ['d', 'b']
    assert {item['id'] for item in transactions} == {'a', 'b', 'c', 'd'}
    assert all(item['category_id'] is None for item in transactions)
    assert first['total_matching'] == second['total_matching'] == 4
    assert first['next_cursor'] is not None
    assert second['next_cursor'] is None
    assert first['sort_by'] == 'amount'
    assert first['sort_order'] == 'desc'


def test_search_supports_category_and_account_arrays():
    db = make_db()
    add_account(db, id='cash', title='Cash', type='checking', balance=1000)
    add_account(db, id='other', title='Other', type='checking', balance=1000)
    db.connect().executemany(
        "INSERT INTO tags(id,title,parent,show_outcome,user,changed) VALUES (?,?,?,1,1,1)",
        [('parent', 'Parent', None), ('child', 'Child', 'parent')],
    )
    add_tx(db, id='match', amount=10, account='other', tag='child')
    add_tx(db, id='wrong-account', amount=20, account='cash', tag='child')
    add_tx(db, id='wrong-category', amount=30, account='other')

    result = search_transactions(
        db,
        category_ids=['parent'],
        account_ids=['other'],
        category_state='categorized',
        sort_order='asc',
    )

    assert [item['id'] for item in result['transactions']] == ['match']
    assert result['transactions'][0]['category_id'] == 'child'


def test_search_rejects_cursor_from_another_sort_contract():
    db = make_db()
    add_account(db, id='cash', title='Cash', type='checking', balance=1000)
    add_tx(db, id='a', amount=10, account='cash')
    add_tx(db, id='b', amount=20, account='cash')
    first = search_transactions(db, sort_by='amount', sort_order='desc', limit=1)

    with pytest.raises(InputValidationError, match='cursor'):
        search_transactions(
            db,
            sort_by='amount',
            sort_order='asc',
            cursor=first['next_cursor'],
            limit=1,
        )


def test_search_date_cursor_is_stable_across_equal_dates():
    db = make_db()
    add_account(db, id='cash', title='Cash', type='checking', balance=1000)
    for tx_id in ('a', 'b', 'c'):
        add_tx(
            db,
            id=tx_id,
            amount=10,
            account='cash',
            tx_date='2026-01-15',
        )

    first = search_transactions(db, sort_by='date', sort_order='asc', limit=2)
    second = search_transactions(
        db,
        sort_by='date',
        sort_order='asc',
        cursor=first['next_cursor'],
        limit=2,
    )

    assert [item['id'] for item in first['transactions']] == ['a', 'b']
    assert [item['id'] for item in second['transactions']] == ['c']


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('category_state', 'missing'),
        ('sort_by', 'merchant'),
        ('sort_order', 'sideways'),
        ('category_ids', 'food'),
        ('account_ids', [1]),
        ('cursor', 'not-a-cursor'),
    ],
)
def test_search_rejects_invalid_pagination_filters(field, value):
    db = make_db()
    add_account(db, id='cash', title='Cash', type='checking', balance=1000)

    with pytest.raises(InputValidationError):
        search_transactions(db, **{field: value})


def test_top_n_bounds_are_rejected_instead_of_silently_clamped():
    db = make_db()
    with pytest.raises(InputValidationError, match='top_n must be between 1 and 100'):
        analyze_spending(db, top_n=0)
    with pytest.raises(InputValidationError, match='top_n must be between 1 and 100'):
        analyze_spending(db, top_n=101)


def test_search_fails_when_candidate_exchange_rate_is_missing():
    db = make_db()
    db.connect().execute(
        "INSERT INTO instruments(id,title,short_title,symbol,rate,changed) VALUES (3,'Broken','BAD','?',0,1)"
    )
    db.connect().commit()
    add_account(db,id='bad',title='Bad FX',type='checking',balance=100,instrument=3)
    add_tx(db,id='bad-rate',amount=100,tx_type='outcome',account='bad',instrument=3,payee='X')
    with pytest.raises(CurrencyRateError, match='zero exchange rate'):
        search_transactions(db, min_amount=1)


def test_validation_rejects_invalid_and_reversed_dates():
    with pytest.raises(InputValidationError):
        resolve_date_range(period='2026-99')
    with pytest.raises(InputValidationError):
        resolve_date_range(start_date='2026-02-02',end_date='2026-02-01')


def test_uncategorized_budget_does_not_count_categorized_expenses():
    db = make_db()
    add_account(db, id='cash', title='Cash', type='checking', balance=1000)
    db.connect().execute(
        'INSERT INTO tags(id,title,parent,show_outcome,budget_outcome,user,changed) VALUES ("food","Food",NULL,1,1,1,1)'
    )
    month = date.today().strftime('%Y-%m-01')
    db.connect().execute(
        '''INSERT INTO budgets(user,tag,date,income,income_lock,outcome,outcome_lock,changed,tag_key)
           VALUES (1,NULL,?,0,0,500,1,1,'')''',
        (month,),
    )
    db.connect().commit()
    add_tx(db, id='uncategorized', amount=100, tag=None)
    add_tx(db, id='categorized', amount=300, tag='food')

    result = check_budget_health(db, month=date.today().strftime('%Y-%m'))
    uncategorized = next(x for x in result['categories'] if x['tag_id'] is None)
    assert uncategorized['actual'] == 100



def test_budget_surfaces_spending_in_categories_without_budget_rows():
    db = make_db()
    add_account(db,id='cash',title='Cash',type='checking',balance=1000)
    db.connect().executemany(
        'INSERT INTO tags(id,title,parent,show_outcome,budget_outcome,user,changed) VALUES (?,?,NULL,1,1,1,1)',
        [('food','Food'),('extra','Extra')],
    )
    month = date.today().strftime('%Y-%m-01')
    db.connect().execute(
        "INSERT INTO budgets(user,tag,date,income,income_lock,outcome,outcome_lock,changed,tag_key) VALUES (1,'food',?,0,0,1000,1,1,'food')",
        (month,),
    )
    db.connect().commit()
    add_tx(db,id='food',amount=100,tag='food')
    add_tx(db,id='extra',amount=300,tag='extra')

    result = check_budget_health(db, month=date.today().strftime('%Y-%m'))
    extra = next(item for item in result['categories'] if item['tag_id'] == 'extra')
    assert extra['planned'] == 0
    assert extra['actual'] == 300
    assert extra['status'] == 'unbudgeted_spending'


def test_budget_uses_marker_currency_when_marker_has_no_account():
    db = make_db()
    db.connect().execute(
        'INSERT INTO tags(id,title,parent,show_outcome,budget_outcome,user,changed) VALUES ("food","Food",NULL,1,1,1,1)'
    )
    month = date.today().strftime('%Y-%m-01')
    db.upsert_budgets([{
        'user': 1, 'tag': 'food', 'date': month, 'income': 0,
        'incomeLock': False, 'outcome': 1000, 'outcomeLock': False, 'changed': 1,
    }])
    db.upsert_reminder_markers([{
        'id': 'usd-plan', 'user': 1, 'date': date.today().isoformat(),
        'state': 'planned', 'income': 0, 'outcome': 10,
        'outcomeInstrument': 2, 'outcomeAccount': None,
        'tag': ['food'], 'changed': 1,
    }])

    result = check_budget_health(db, month=date.today().strftime('%Y-%m'))
    food = next(item for item in result['categories'] if item['tag_id'] == 'food')
    assert food['planned'] == 1900


def test_currency_tools_reject_malformed_codes_and_lists():
    db = make_db()
    with pytest.raises(InputValidationError, match='from_currency'):
        convert_currency(db, 100, '', 'RUB')
    with pytest.raises(InputValidationError, match='to_currency'):
        convert_currency(db, 100, 'USD', None)
    with pytest.raises(InputValidationError, match='currencies'):
        get_exchange_rates(db, currencies=['USD', 123])
    with pytest.raises(InputValidationError, match='currencies'):
        get_exchange_rates(db, currencies=[])


def test_currency_results_use_conservative_rate_source_description():
    db = make_db()
    converted = convert_currency(db, 1, 'USD', 'RUB')
    rates = get_exchange_rates(db, currencies=['USD', 'RUB'])
    assert converted['rate_source'] == 'ZenMoney synchronized instrument data'
    assert rates['rate_source'] == 'ZenMoney synchronized instrument data'
    assert 'Central Bank' not in rates.get('note', '')


def test_nested_budgets_do_not_double_count_child_actual_or_planned_total():
    db = make_db()
    add_account(db, id="cash", title="Cash", type="checking", balance=1000)
    db.connect().executemany(
        "INSERT INTO tags(id,title,parent,show_outcome,budget_outcome,user,changed) "
        "VALUES (?,?,?,1,1,1,1)",
        [("food", "Food", None), ("groceries", "Groceries", "food")],
    )
    month = date.today().strftime("%Y-%m-01")
    db.upsert_budgets(
        [
            {
                "user": 1,
                "tag": "food",
                "date": month,
                "income": 0,
                "incomeLock": False,
                "outcome": 1000,
                "outcomeLock": True,
                "changed": 1,
            },
            {
                "user": 1,
                "tag": "groceries",
                "date": month,
                "income": 0,
                "incomeLock": False,
                "outcome": 300,
                "outcomeLock": True,
                "changed": 2,
            },
        ]
    )
    add_tx(db, id="grocery", amount=200, tag="groceries")

    result = check_budget_health(db, month=date.today().strftime("%Y-%m"))
    by_tag = {item["tag_id"]: item for item in result["categories"]}

    assert by_tag["food"]["actual"] == 0
    assert by_tag["groceries"]["actual"] == 200
    assert result["overall"]["actual"] == 200
    # The parent cap already contains the child allocation.
    assert result["overall"]["planned"] == 1000


def test_total_only_budget_uses_total_cap_and_all_actual_spending():
    db = make_db()
    add_account(db, id="cash", title="Cash", type="checking", balance=1000)
    month = date.today().strftime("%Y-%m-01")
    db.upsert_budgets(
        [
            {
                "user": 1,
                "tag": "00000000-0000-0000-0000-000000000000",
                "date": month,
                "income": 0,
                "incomeLock": False,
                "outcome": 1200,
                "outcomeLock": True,
                "changed": 1,
            }
        ]
    )
    add_tx(db, id="expense", amount=200, tag=None)

    result = check_budget_health(db, month=date.today().strftime("%Y-%m"))

    assert result["overall"]["planned"] == 1200
    assert result["overall"]["actual"] == 200
    assert result["overall"]["status"] == "on_track"


def test_exchange_rates_reject_unknown_requested_currency():
    db = make_db()
    with pytest.raises(CurrencyRateError, match="UNKNOWN"):
        get_exchange_rates(db, currencies=["UNKNOWN"])


def test_spending_reports_excluded_holds_in_user_currency():
    db = make_db()
    add_account(db, id="usd", title="USD", type="checking", balance=100, instrument=2)
    add_tx(db, id="posted", amount=10, account="usd", instrument=2, hold=0)
    add_tx(db, id="pending", amount=5, account="usd", instrument=2, hold=1)

    result = analyze_spending(db, period="this_month")

    assert result["total_outcome"] == 900
    assert result["holds_excluded"] == {"amount": 450.0, "count": 1}

    with_holds = analyze_spending(db, period="this_month", include_holds=True)
    assert with_holds["total_outcome"] == 1350
    assert with_holds["holds_excluded"] is None


def test_explicit_end_date_requires_start_date():
    with pytest.raises(InputValidationError, match="end_date requires start_date"):
        resolve_date_range(end_date="2026-08-21")


def test_net_worth_rounds_only_after_summing_unrounded_buckets():
    db = make_db()
    add_account(db, id="cash", title="Cash", type="checking", balance=0.006)
    add_account(db, id="save", title="Save", type="deposit", balance=0.006)

    result = get_net_worth(db)

    assert result["breakdown"]["current"]["total"] == 0.01
    assert result["breakdown"]["savings"]["total"] == 0.01
    assert result["net_worth"] == 0.01
    assert result["net_worth_all_accounts"] == 0.01


def test_numeric_validation_rejects_nan_and_infinity():
    db = make_db()
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(InputValidationError, match="non-negative number"):
            convert_currency(db, value, "USD", "RUB")


def test_budget_matches_all_json_tags_without_reporting_covered_spend_as_unbudgeted():
    db = make_db()
    add_account(db, id="cash", title="Cash", type="checking", balance=1000)
    db.connect().executemany(
        "INSERT INTO tags(id,title,parent,show_outcome,budget_outcome,user,changed) "
        "VALUES (?,?,NULL,1,1,1,1)",
        [("other", "Other"), ("food", "Food")],
    )
    month = date.today().strftime("%Y-%m-01")
    db.upsert_budgets(
        [
            {
                "user": 1,
                "tag": "food",
                "date": month,
                "outcome": 100,
                "outcomeLock": False,
                "changed": 1,
            }
        ]
    )
    db.upsert_reminder_markers(
        [
            {
                "id": "planned",
                "user": 1,
                "date": date.today().isoformat(),
                "state": "planned",
                "outcome": 50,
                "outcomeInstrument": 1,
                "tag": ["other", "food"],
                "changed": 1,
            }
        ]
    )
    add_tx(db, id="actual", amount=40, tag="other")
    db.connect().execute(
        "UPDATE transactions SET tag=? WHERE id='actual'",
        (json.dumps(["other", "food"]),),
    )
    db.connect().commit()

    result = check_budget_health(db, month=date.today().strftime("%Y-%m"))
    food = next(item for item in result["categories"] if item["tag_id"] == "food")

    assert food["planned"] == 150
    assert food["actual"] == 40
    assert not any(
        item["tag_id"] == "other" and item["status"] == "unbudgeted_spending"
        for item in result["categories"]
    )


def test_account_flow_normalizes_debt_movement_to_account_currency():
    db = make_db()
    add_account(db, id="debt", title="Debt", type="debt", balance=900, instrument=1)
    add_account(db, id="usd", title="USD", type="checking", balance=10, instrument=2)
    add_tx(
        db,
        id="borrowed",
        amount=10,
        tx_type="transfer_out",
        account="debt",
        instrument=2,
        other_account="usd",
        other_instrument=2,
        other_amount=10,
    )

    result = get_account_flow(db, "debt", "this_month")
    transaction = result["transactions"][0]

    assert transaction["amount"] == 900
    assert transaction["currency"] == "RUB"
    assert transaction["amount_converted"] == 900
    assert result["summary"]["transfer_out"] == 900
    assert result["summary"]["net_change"] == -900


def test_budget_rejects_nonzero_marker_without_currency_metadata():
    db = make_db()
    db.connect().execute(
        'INSERT INTO tags(id,title,parent,show_outcome,budget_outcome,user,changed) '
        'VALUES ("food","Food",NULL,1,1,1,1)'
    )
    db.upsert_budgets(
        [
            {
                "user": 1,
                "tag": "food",
                "date": date.today().strftime("%Y-%m-01"),
                "outcome": 100,
                "outcomeLock": False,
                "changed": 1,
            }
        ]
    )
    db.upsert_reminder_markers(
        [
            {
                "id": "missing-currency",
                "user": 1,
                "date": date.today().isoformat(),
                "state": "planned",
                "outcome": 50,
                "tag": ["food"],
                "changed": 1,
            }
        ]
    )

    with pytest.raises(FinancialDataError, match="currency instrument"):
        check_budget_health(db, month=date.today().strftime("%Y-%m"))


def test_liquidity_reports_checking_account_credit_as_borrowing_capacity():
    db = make_db()
    add_account(
        db,
        id="overdraft",
        title="Overdraft",
        type="checking",
        balance=-200,
        credit_limit=1000,
    )

    result = get_liquidity(db)

    assert result["liquid_own"] == 0
    assert result["credit_available"] == 800
    assert result["total_spendable_with_credit"] == 800


def test_last_30_days_is_an_inclusive_30_day_window():
    start, end = resolve_date_range("last_30_days")

    assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 29
