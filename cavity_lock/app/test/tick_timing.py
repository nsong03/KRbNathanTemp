import asyncio, json, time, sys
import requests
from playwright.async_api import async_playwright
PORT = int(sys.argv[1])
async def grab():
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await b.new_page()
        got = {}
        def on_req(r):
            if "_dash-update-component" in r.url and '"tick"' in (r.post_data or "") and "body" not in got:
                got["body"] = r.post_data; got["url"] = r.url
        pg.on("request", on_req)
        await pg.goto(f"http://127.0.0.1:{PORT}"); await pg.wait_for_timeout(3000)
        await b.close()
        return got
got = asyncio.run(grab())
body = json.loads(got["body"])
times, sizes = [], []
for i in range(20):
    t0 = time.time()
    r = requests.post(got["url"], json=body, headers={"Content-Type": "application/json"})
    times.append(time.time() - t0); sizes.append(len(r.content))
print(f"tick callback: median {sorted(times)[10]*1e3:.0f} ms, max {max(times)*1e3:.0f} ms, response {sorted(sizes)[10]/1e3:.0f} kB")
