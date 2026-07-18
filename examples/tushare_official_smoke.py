#!/usr/bin/env python3
"""Minimal official Tushare Pro call without a third-party endpoint override."""

import os

import tushare as ts


token = os.environ["TUSHARE_TOKEN"]
pro = ts.pro_api(token)

print(pro.index_basic(limit=5))
print(ts.pro_bar(api=pro, ts_code="000001.SZ", limit=3))
