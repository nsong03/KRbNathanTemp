import asyncio
from playwright.async_api import async_playwright
PORT = 8070
JS_PEAKS = """() => { const gd = document.querySelector('#scope .js-plotly-plot');
  return gd.data.filter(d => d.mode === 'markers+text').map(d => ({x: d.x, y: d.y, sym: d.marker.symbol})); }"""
JS_PIX = """([t, y]) => { const gd = document.querySelector('#scope .js-plotly-plot');
  const xa = gd._fullLayout.xaxis, ya = gd._fullLayout.yaxis, bb = gd.getBoundingClientRect();
  return [bb.left + xa._offset + xa.l2p(t), bb.top + ya._offset + ya.l2p(y)]; }"""
JS_FPS = """() => new Promise(res => { const gd = document.querySelector('#scope .js-plotly-plot'); let n = 0;
  gd.on('plotly_afterplot', () => n++); setTimeout(() => res(n / 5), 5000); })"""
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1500, "height": 1000})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await pg.goto(f"http://127.0.0.1:{PORT}"); await pg.wait_for_timeout(3500)
        print("browser redraws/s while acquiring:", await pg.evaluate(JS_FPS))
        await pg.screenshot(path="v3_0_acquire.png")
        peaks = await pg.evaluate(JS_PEAKS)
        other = [q for q in peaks if q["sym"] != "star"][0]
        px = await pg.evaluate(JS_PIX, [other["x"][0], other["y"][0] * 0.5])
        await pg.mouse.click(*px); await pg.wait_for_timeout(1200)
        print("target after click:", await pg.locator("#target-text").inner_text())
        await pg.click("#btn-tallest"); await pg.wait_for_timeout(800)
        await pg.click("#btn-phase"); await pg.wait_for_timeout(6000)
        await pg.click("#s-lock_same_dither input")
        for k, v in (("lock_dither_mvpp", "4"), ("lock_dither_hz", "2000"), ("noise_avg", "2")):
            loc = pg.locator(f"#s-{k}"); await loc.fill(v); await loc.press("Enter"); await pg.wait_for_timeout(400)
        await pg.click("#btn-lock"); await pg.wait_for_timeout(9000)
        print("status:", await pg.locator("#status").inner_text())
        print("browser redraws/s while locked:", await pg.evaluate(JS_FPS))
        await pg.screenshot(path="v3_1_locked.png")
        await pg.click("text= noise spectrum"); await pg.wait_for_timeout(300)
        await pg.click("text= loop (Bode)"); await pg.wait_for_timeout(300)
        await pg.click("#btn-noise"); await pg.wait_for_timeout(7000)
        await pg.click("#btn-loop"); await pg.wait_for_timeout(5000)
        await pg.screenshot(path="v3_2_diag.png", full_page=True)
        print("analysis:", (await pg.locator("#analysis").inner_text())[-400:])
        print("log head:\n" + "\n".join((await pg.locator("#log").inner_text()).splitlines()[:12]))
        print("browser errors:", errors[:5])
        await b.close()
asyncio.run(main())
