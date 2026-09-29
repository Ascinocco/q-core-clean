from datetime import date
import copy
import json

import pytest

from api.forecast_model import Plan, Rule, forecast, occurrences

from api.tests.reader_routes import assert_reader_only_get

# split part 2: the read-only routes now need a credential like every route.
READER = {"Authorization": "Bearer test-token"}


def plan_data():
    return {'accounts':[
        {'key':'main','label':'Main','kind':'cash','balance_cents':1000000,'as_of':'2026-08-31','evidence':'Synthetic'},
        {'key':'runway','label':'Runway','kind':'cash','balance_cents':500000,'as_of':'2026-08-31','evidence':'Synthetic'},
        {'key':'card','label':'Card','kind':'debt','balance_cents':200000,'as_of':'2026-08-31','evidence':'Synthetic'}],
        'runway_account':'runway','snowball_rule':'payment','rules':[
        {'key':'salary','label':'Salary','kind':'income','account':'main','amount_cents':500000,'days':[15,31],'start':'2026-09-01','confidence':'confirmed','evidence':'Synthetic'},
        {'key':'transfer','label':'Transfer','kind':'transfer','account':'main','destination':'runway','amount_cents':300000,'days':[15,31],'start':'2026-09-01','confidence':'confirmed','evidence':'Synthetic'},
        {'key':'mortgage','label':'Mortgage','kind':'expense','account':'runway','amount_cents':180000,'days':[1,15],'start':'2026-09-01','confidence':'estimated','evidence':'Synthetic','property_label':'Home'},
        {'key':'groceries','label':'Groceries','kind':'expense','account':'card','amount_cents':30000,'cadence':'daily_budget','start':'2026-09-01','confidence':'estimated','evidence':'Synthetic'},
        {'key':'payment','label':'Payment','kind':'debt_payment','account':'main','destination':'card','amount_cents':10000,'days':[24],'start':'2026-09-01','confidence':'estimated','evidence':'Synthetic'}]}


def run(value=None, **kwargs):
    return forecast(Plan.model_validate(value or plan_data()),date(2026,9,30),date(2026,9,1),**kwargs)


def test_balances_conserve_transfers_and_never_double_count_card_spending():
    result=run();end=result['daily'][-1]['balances'];month=result['months'][-1]
    assert end=={'main':1390000,'runway':740000,'card':220000}
    assert month['income_cents']==1000000
    assert month['running_costs_cents']==390000
    assert month['debt_payments_cents']==10000
    assert month['runway_growth_cents']==240000
    assert month['complete_month']
    assert result['lowest_projected_cash']['runway']['amount_cents']==140000
    assert {e['cadence'] for e in result['events']} >= {'monthly','daily_budget'}


def test_extra_payment_and_allowance_are_nonpersistent_scenarios():
    p=Plan.model_validate(plan_data());before=p.model_dump()
    result=forecast(p,date(2026,9,30),date(2026,9,1),50000,date(2026,9,10),20000)
    assert result['daily'][-1]['balances']['main']==1330000
    assert result['daily'][-1]['balances']['card']==160000
    assert p.model_dump()==before


def test_unknown_balances_and_payments_never_become_zero_certainty():
    value=plan_data();value['accounts'][2]['balance_cents']=None
    value['rules'][-1].update(amount_cents=None,confidence='unknown')
    result=run(value)
    assert result['daily'][-1]['balances']['card'] is None
    assert result['unknown_payments']==['Payment']
    assert result['months'][-1]['debt_payments_cents']==0


def test_debt_snapshot_resets_only_at_its_date_not_double_counting_earlier_activity():
    value=plan_data();value['accounts'][2]['as_of']='2026-09-15'
    result=run(value)
    assert result['daily'][1]['balances']['card'] is None
    assert result['daily'][-1]['balances']['card']==205000


@pytest.mark.parametrize('month,days',[(2,28),(4,30),(12,31)])
def test_daily_budget_preserves_exact_monthly_cents(month,days):
    r=Rule.model_validate({**plan_data()['rules'][3],'amount_cents':10001,'start':'2026-01-01'})
    values=list(occurrences(r,date(2026,month,1),date(2026,month,days)))
    assert len(values)==days and sum(v for _,v in values)==10001


def test_month_end_leap_year_and_weekend_boundary():
    r=Rule.model_validate({**plan_data()['rules'][0],'days':[31],'start':'2024-01-01'})
    assert list(occurrences(r,date(2024,2,1),date(2024,2,29)))[0][0]==date(2024,2,29)
    r=r.model_copy(update={'days':[1],'weekend':'previous'})
    assert list(occurrences(r,date(2026,7,31),date(2026,7,31)))[0][0]==date(2026,7,31)


@pytest.mark.parametrize('change', ['fractional','bad_destination','duplicate','missing_confidence','mixed_cash_dates'])
def test_invalid_plans_refuse(change):
    value=plan_data()
    if change=='fractional':value['rules'][0]['amount_cents']=1.1
    if change=='bad_destination':value['rules'][1]['destination']='card'
    if change=='duplicate':value['rules'].append(value['rules'][0])
    if change=='missing_confidence':value['rules'][0]['amount_cents']=None
    if change=='mixed_cash_dates':value['accounts'][0]['as_of']='2026-08-30'
    with pytest.raises(ValueError):Plan.model_validate(value)


def test_invalid_scenario_date_refuses():
    with pytest.raises(ValueError):run(extra_payment_cents=100,extra_payment_date=date(2026,8,31))


def test_plan_api_authenticated_versioned_and_projection_read_only(client,test_settings):
    from pathlib import Path
    auth={'Authorization':f'Bearer {test_settings.api_token}'}
    payload={'plan':plan_data(),'actor':'test','note':'Synthetic plan'}
    assert client.get('/forecast/projection', headers=READER).status_code==404
    assert client.put('/forecast/plan',json=payload).status_code==401
    first=client.put('/forecast/plan',json=payload,headers=auth)
    assert first.status_code==200,first.text
    revision=first.json()['revision']
    assert client.get('/forecast/plan').status_code==401
    assert client.put('/forecast/plan',json=payload,headers=auth).status_code==409
    payload.update(expected_revision=revision,note='Revised')
    second=client.put('/forecast/plan',json=payload,headers=auth)
    assert second.status_code==200
    root=Path(test_settings.forecast_dir)
    assert (root/(revision+'.json')).exists()
    assert (root/'current.json').stat().st_mode&0o777==0o600
    before=(root/'current.json').read_bytes()
    response=client.get('/forecast/projection', headers=READER)
    assert response.status_code==200,response.text
    assert response.headers['cache-control']=='no-store'
    assert (root/'current.json').read_bytes()==before
    assert client.get('/ui/forecast', headers=READER).status_code==200
    assert client.post('/forecast/projection',json={},headers=READER).status_code==405


def test_partial_month_and_negative_runway_are_visible():
    value=plan_data();value['accounts'][1]['balance_cents']=0
    result=run(value)
    assert result['lowest_projected_cash']['runway']['amount_cents']<0
    assert result['months'][0]['complete_month'] is False


def test_corrupt_plan_and_invalid_scenario_have_safe_conflicts(client,test_settings):
    from pathlib import Path
    root=Path(test_settings.forecast_dir);root.mkdir()
    (root/'current.json').write_text('{invalid private contents')
    result=client.get('/forecast/projection', headers=READER)
    assert result.status_code==409
    assert 'private contents' not in result.text
    (root/'current.json').unlink()
    auth={'Authorization':f'Bearer {test_settings.api_token}'}
    client.put('/forecast/plan',headers=auth,json={'plan':plan_data(),'actor':'test','note':'Synthetic'})
    assert client.get('/forecast/projection?extra_payment_cents=100&extra_payment_date=2020-01-01', headers=READER).status_code==409


def test_public_projection_never_follows_plan_symlink_or_reads_oversized_file(client,test_settings,tmp_path):
    from pathlib import Path
    root=Path(test_settings.forecast_dir);root.mkdir()
    secret=tmp_path/'secret';secret.write_text('operator secret')
    (root/'current.json').symlink_to(secret)
    response=client.get('/forecast/projection', headers=READER)
    assert response.status_code==409 and 'operator secret' not in response.text
    (root/'current.json').unlink()
    (root/'current.json').write_text('x'*262145)
    assert client.get('/forecast/projection', headers=READER).status_code==409


def test_snapshot_on_first_is_still_a_partial_month():
    value=plan_data()
    for a in value['accounts']:a['as_of']='2026-09-01'
    assert run(value)['months'][0]['complete_month'] is False





def test_forecast_read_routes_need_a_reader_and_are_get_only(client):
    from api.forecast import router
    readers = [r for r in router.routes if r.path in {'/ui/forecast', '/forecast/projection'}]
    assert len(readers) == 2
    assert_reader_only_get(readers)
    assert client.get('/forecast/projection').status_code == 401
