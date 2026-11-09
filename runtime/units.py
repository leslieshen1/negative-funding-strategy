"""Exact raw-token quantum; exchange and spot steps need a common multiple."""
from decimal import Decimal
from math import lcm

def paired_token_step(spot_step,perp_exchange_step,tokens_per_perp_quantity):
 a=Decimal(str(spot_step));b=Decimal(str(perp_exchange_step))*Decimal(str(tokens_per_perp_quantity))
 if not a.is_finite() or not b.is_finite() or a<=0 or b<=0:raise ValueError('positive finite steps required')
 scale=10**max(0,-a.as_tuple().exponent,-b.as_tuple().exponent)
 return Decimal(lcm(int(a*scale),int(b*scale)))/Decimal(scale)
