"""Offline treasury/risk overlay. Emits intentions, never orders or transfers."""
from decimal import Decimal as D, InvalidOperation
import argparse, hashlib, json, sys, time
from pathlib import Path
from schema_check import shape,strict_load

ZERO=D(0); SCHEMA='nfb.treasury-input.v1'; OUTPUT='nfb.treasury-output.v1'
class InputError(ValueError): pass
def money(x):
    if not isinstance(x,str):raise InputError('Amounts must be decimal strings')
    try:v=D(x)
    except InvalidOperation:raise InputError('Invalid decimal')
    if not v.is_finite():raise InputError('Non-finite decimal')
    return v
def s(x):return format(x,'f')
def positive(x):return max(ZERO,x)

def product_collateral(row):
    if row['status']!='SETTLED':return ZERO
    if not row['eligibility_verified']:return ZERO
    value=money(row['quantity'])*money(row['nav_usdt_per_unit'])
    if value<0:raise InputError('Negative asset value')
    tiers=row.get('tiers',[])
    if tiers:
        total=ZERO; previous=ZERO
        for tier in tiers:
            ceiling=money(tier['ceiling_usdt']); ratio=money(tier['collateral_ratio'])
            if ceiling<=previous or not ZERO<=ratio<=1:raise InputError('Invalid collateral tiers')
            total+=max(ZERO,min(value,ceiling)-previous)*ratio
            previous=ceiling
        if value>previous:raise InputError('Uncovered collateral tier')
        return total
    ratio=money(row['collateral_ratio'])
    if not ZERO<=ratio<=1:raise InputError('Invalid collateral ratio')
    return value*ratio

def decide(request,now_ms=None):
    try:shape(request,json.loads((Path(__file__).parent/'schemas/treasury-input.schema.json').read_text()))
    except ValueError as exc:raise InputError(str(exc))
    if request.get('schema_version')!=SCHEMA:raise InputError('Unsupported schema_version')
    now=int(time.time()*1000) if now_ms is None else now_ms
    asof=request['as_of_ms']; version=request['ledger_version']
    if isinstance(asof,bool) or not isinstance(asof,int) or isinstance(version,bool) or not isinstance(version,int) or version<0:raise InputError('Invalid time or ledger version')
    if not asof<=now<asof+60000:raise InputError('STALE_OR_FUTURE_REQUEST')
    if request['account_mode'] not in ['USDT_SINGLE','MULTI_ASSETS','PM','PM_PRO']:raise InputError('Unknown account mode')
    if request['account_kind'] not in ['MASTER','REGULAR_SUB']:raise InputError('Unknown account kind')
    a=request['account_a']; b=request['account_b']; policy=request['policy']
    received=request['snapshot_received_at_ms']
    flags=[]; intents=[]; locked=[]
    if not 0<=now-received<=10000:flags.append('STALE_ACCOUNT_SNAPSHOT')
    if request['source_mode'] not in ['synthetic','observed']:raise InputError('Unknown source mode')
    products=request['yield_assets']; b_products=ZERO; a_products=ZERO; product_keys=set()
    for row in products:
        if row['account'] not in ['A','B']:raise InputError('Unknown asset account')
        quantity=money(row['quantity']); nav=money(row['nav_usdt_per_unit'])
        key=(row['account'],row['asset'])
        if key in product_keys:raise InputError('Duplicate yield asset would double-count collateral')
        product_keys.add(key)
        if quantity<0 or nav<=0:raise InputError('Invalid yield quantity or NAV')
        if row['status'] not in ['SETTLED','REDEEMING','PENDING']:raise InputError('Unknown asset status')
        fresh=0<=now-row['observed_at_ms']<=10000 and now<row['valid_until_ms']
        if not fresh:flags.append('STALE_YIELD_QUOTE')
        if not row['eligibility_verified']:flags.append('YIELD_PERMISSION_OR_COLLATERAL_UNVERIFIED')
        if row['asset']=='LDUSDT' and request['account_kind']=='REGULAR_SUB' and not request['subaccount_conversion_route_verified']:flags.append('SUBACCOUNT_MASTER_CONVERSION_ROUTE_UNVERIFIED')
        compatible=request['account_mode'] in ['MULTI_ASSETS','PM','PM_PRO']
        if request['account_mode']=='PM_PRO' and not row.get('tiers'):
            flags.append('PM_PRO_TIERED_HAIRCUT_REQUIRED');compatible=False
        if row['account']=='B' and not compatible:flags.append('ACCOUNT_MODE_NOT_SUPPORT_YIELD_COLLATERAL')
        if row['status']=='SETTLED' and fresh and (row['account']=='A' or compatible):
            collateral=product_collateral(row)
            if row['account']=='B':b_products+=collateral
            else:a_products+=collateral
    a_cash=money(a['settled_usdt']); b_cash=money(b['settled_usdt']); reserved_a=money(a['reserved_usdt']); reserved_b=money(b['reserved_usdt'])
    if min(a_cash,b_cash)<0:flags.append('NEGATIVE_SETTLED_USDT_REQUIRES_REPAIR')
    # Assets in A, pending transfers and pending redemptions never collateralize B.
    local_b_equity=b_cash+b_products+money(b['unrealized_pnl_usdt'])-money(b['other_liabilities_usdt'])
    reported=b['exchange_adjusted_equity_usdt']
    if reported is None:flags.append('EXCHANGE_EQUITY_UNKNOWN'); conservative_b_equity=local_b_equity
    else:conservative_b_equity=min(local_b_equity,money(reported))
    notional=money(b['position_notional_usdt']); margin_gap=positive(notional-conservative_b_equity)
    if margin_gap>0:flags.append('ONE_X_COLLATERAL_SHORTFALL')
    a_buyback=ZERO; instruments=[]
    for row in request['instruments']:
        sold=money(row['sold_spot_qty_token']); perp=money(row['perp_long_qty_token']); borrowed=money(row['borrowed_qty_token']); returned=money(row['provider_confirmed_return_qty_token'])
        if min(sold,perp,borrowed,returned)<0 or returned>borrowed:raise InputError('Invalid coin obligation')
        if row['spot_ask_usdt_per_token'] is None:
            flags.append('BUYBACK_QUOTE_UNKNOWN'); buyback=None
        else:
            px=money(row['spot_ask_usdt_per_token'])
            if px<=0:raise InputError('Invalid spot price')
            buyback=sold*px*(1+money(policy['spot_fee_rate']))*money(policy['a_buyback_stress_multiple']); a_buyback+=buyback
        risk=set(row['risk_events'])
        forced=bool(risk & {'BORROW_RECALL','ADL','LIQUIDATION','DELISTING','TRADING_HALT'})
        exit_locked=row['exit_locked'] or forced
        if exit_locked:locked.append(row['instrument_id'])
        if sold!=perp:flags.append('HEDGE_IMBALANCE'); intents.append({'action':'RECONCILE_HEDGE','instrument_id':row['instrument_id'],'gap_token':s(perp-sold),'quantity_in_token':True})
        if exit_locked:intents.append({'action':'LOCK_EXIT','instrument_id':row['instrument_id'],'desired_pair_qty_token':'0','reason_codes':sorted(risk) or ['EXISTING_EXIT_LOCK']})
        if not row['rehypothecation_authorized']:
            intents.append({'action':'KEEP_IDLE_COINS_UNENCUMBERED','instrument_id':row['instrument_id'],'reason_codes':['LENDER_PERMISSION_NOT_CONFIRMED']})
        instruments.append({'instrument_id':row['instrument_id'],'exit_locked':exit_locked,'strategy_buyback_qty_token':s(sold),'provider_outstanding_qty_token':s(borrowed-returned),'provider_return_verified':returned==borrowed,'buyback_stressed_usdt':None if buyback is None else s(buyback)})
    a_gap=positive(a_buyback+reserved_a-a_cash)
    liquid_a=positive(a_cash-reserved_a); liquid_b=positive(b_cash-reserved_b)
    withdraw=b['exchange_max_withdraw_usdt']
    if withdraw is None:flags.append('EXCHANGE_WITHDRAWABLE_UNKNOWN'); withdraw=ZERO
    else:withdraw=positive(money(withdraw))
    # Do not turn a positive unrealized PnL into transferable cash.
    initial_principal=positive(notional-money(b['unrealized_pnl_usdt']))
    transfer_b=min(liquid_b,withdraw,positive(b_cash+b_products-initial_principal-reserved_b))
    if a_gap>0:
        flags.append('A_BUYBACK_BUFFER_SHORTFALL')
        needed=min(a_gap,transfer_b)
        if needed>0 and not request['pending_transfers']:
            intents.append({'action':'REQUEST_TRANSFER_WAIT_FOR_SETTLEMENT','from_account':'B','to_account':'A','asset':'USDT','amount':s(needed)})
        else:intents.append({'action':'WAIT_BUYBACK_CASH','amount':s(a_gap),'reason_codes':['NO_SETTLED_TRANSFERABLE_CASH_OR_TRANSFER_ALREADY_PENDING']})
        if a_gap>transfer_b:
            for asset in products:
                if asset['status']=='SETTLED' and asset['eligibility_verified']:
                    intents.append({'action':'REDEEM_TO_CASH_WAIT_QUOTE_AND_SETTLEMENT','from_account':asset['account'],'asset':asset['asset'],'amount':s(min(a_gap-transfer_b,money(asset['quantity'])*money(asset['nav_usdt_per_unit']))),'reason_codes':['REDEMPTION_FEE_QUOTA_AND_SETTLEMENT_QUOTE_REQUIRED']})
    if margin_gap>0:
        amount=min(margin_gap,positive(liquid_a-a_buyback))
        intents.append({'action':'REQUEST_MARGIN_REPAIR_WAIT','from_account':'A','to_account':'B','asset':'USDT','amount':s(amount),'unfunded_after_intent_usdt':s(margin_gap-amount)})
    if request['pending_redemptions']:intents.append({'action':'WAIT_REDEMPTION_SETTLEMENT','reason_codes':['PENDING_IS_NEITHER_CASH_NOR_COLLATERAL']})
    available=b['exchange_available_for_new_initial_margin_usdt']
    if available is None:flags.append('EXCHANGE_NEW_ORDER_MARGIN_UNKNOWN'); available=ZERO
    else:available=positive(money(available))
    collateral_room=positive(conservative_b_equity-notional-money(policy['b_collateral_buffer_usdt'])-reserved_b)
    proposed_room=min(collateral_room,available,positive(money(request['rc2_new_exposure_budget_usdt'])))
    allowed=not flags and not locked
    payload=json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False)
    expires=min([asof+60000,received+10001]+[min(x['observed_at_ms']+10001,x['valid_until_ms']) for x in products])
    return {'schema_version':OUTPUT,'request_id':request['request_id'],'decision_id':'treasury-'+hashlib.sha256(payload.encode()).hexdigest()[:32],'as_of_ms':asof,'valid_until_ms':expires,'ledger_version':version,'research_only':True,'submission_allowed':False,'rc2_compatible_tightening_only':True,'status':'PAUSE_NEW' if not allowed else 'ALLOW_WITHIN_RC2','allow_new_exposure':allowed,'max_new_exposure_usdt':s(proposed_room if allowed else ZERO),'reason_codes':sorted(set(flags)),'account_b':{'local_collateral_equity_usdt':s(local_b_equity),'conservative_equity_usdt':s(conservative_b_equity),'one_x_gap_usdt':s(margin_gap),'transferable_settled_usdt':s(transfer_b),'a_collateral_used_usdt':'0','pending_used_usdt':'0'},'account_a':{'stressed_buyback_cash_usdt':s(a_buyback),'buyback_buffer_gap_usdt':s(a_gap)},'instruments':instruments,'intents':intents,'execution_actions':[]}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--input',required=True);ap.add_argument('--replay',action='store_true');args=ap.parse_args()
    try:
        r=strict_load(args.input)
        out=decide(r,now_ms=r['as_of_ms'] if args.replay else None)
        print(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
    except (InputError,KeyError,TypeError,InvalidOperation) as e:
        print(json.dumps({'schema_version':OUTPUT,'status':'INVALID_INPUT','submission_allowed':False,'error':str(e)},ensure_ascii=False));sys.exit(2)
if __name__=='__main__':main()
