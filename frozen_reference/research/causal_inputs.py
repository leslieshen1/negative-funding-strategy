"""Past-only inputs shared by the real study and boundary checks."""
import numpy as np
import pandas as pd

def causal_sigma(close):
 logret=np.log(pd.DataFrame(close).where(lambda x:x>0)).diff()
 sd=logret.rolling(24,min_periods=12).std().shift(1).to_numpy()*np.sqrt(24.)
 pool=pd.DataFrame(sd).median(axis=1).ffill().fillna(.05).to_numpy()
 return np.where(np.isfinite(sd),sd,pool[:,None])
