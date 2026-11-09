"""Offline reference checks for this contract's JSON Schema subset and invariants.

Standard library only. Not a general JSON Schema implementation or strategy engine.
The three schemas are independently checked with Draft202012Validator in QA.
"""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parent
KINDS={'strategy_input':'strategy-input','strategy_output':'strategy-output','execution_report':'execution-report'}


def require(ok,reason):
    if not ok:raise ValueError(reason)


def strict_load(path):
    def pairs(items):
        out={}
        for k,v in items:
            require(k not in out,'duplicate JSON key:'+k);out[k]=v
        return out
    def bad(value):raise ValueError('non-finite JSON value:'+value)
    return json.loads(Path(path).read_text(),object_pairs_hook=pairs,parse_constant=bad)


def shape(value,schema,path='$'):
    if 'anyOf' in schema:
        for choice in schema['anyOf']:
            try:shape(value,choice,path);return
            except ValueError:pass
        raise ValueError(path+': no allowed type matched')
    if 'const' in schema:require(value==schema['const'],path+': constant mismatch')
    if 'enum' in schema:require(value in schema['enum'],path+': enum mismatch')
    types=schema.get('type');types=types if isinstance(types,list) else [types] if types else []
    predicates={'object':lambda v:isinstance(v,dict),'array':lambda v:isinstance(v,list),
                'string':lambda v:isinstance(v,str),'integer':lambda v:type(v) is int,
                'boolean':lambda v:type(v) is bool,'null':lambda v:v is None}
    if types:require(any(predicates[t](value) for t in types),path+': wrong type')
    if isinstance(value,dict):
        props=schema.get('properties',{})
        for key in schema.get('required',[]):require(key in value,path+': missing '+key)
        if schema.get('additionalProperties') is False:require(not(set(value)-set(props)),path+': unknown field')
        for key,v in value.items():
            if key in props:shape(v,props[key],path+'.'+key)
    elif isinstance(value,list):
        for i,v in enumerate(value):shape(v,schema['items'],path+f'[{i}]')
    elif isinstance(value,str):
        if 'minLength' in schema:require(len(value)>=schema['minLength'],path+': too short')
        if 'pattern' in schema:require(re.fullmatch(schema['pattern'],value) is not None,path+': invalid numeric/string format')
    elif type(value) is int and 'minimum' in schema:require(value>=schema['minimum'],path+': below minimum')


