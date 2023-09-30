#!/usr/bin/env python3

import asyncio
import time
import datetime
from huawei_solar import HuaweiSolarBridge

async def test():
    bridge = await HuaweiSolarBridge.create(host="192.168.1.51", port=502, slave_id=1)
    while (True): 
        print(datetime.datetime.now())
        print(await bridge.update())
        time.sleep(30)

## main
if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while (True):
        try:
            loop.run_until_complete(test())
        except Exception as e:
            print (e)

