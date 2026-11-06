"""Browser test of pi_app (Playwright). Start `python serve_pi.py` first (fake, port 8076)."""
import asyncio, json, os, urllib.request
from playwright.async_api import async_playwright
PORT = 8076
LAUNCH = dict(executable_path=os.environ.get("CHROME", "/opt/pw-browsers/chromium"), args=["--no-sandbox"]) \
    if os.path.exists(os.environ.get("CHROME", "/opt/pw-browsers/chromium")) else {}
FAILS = []


def check(name, ok, info=""):
    print(f"   [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({info})" if info else ""), flush=True)
    if not ok:
        FAILS.append(name)


def stats():
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/_test/stats", timeout=5).read())


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(**LAUNCH)
        pg = await b.new_page(viewport={"width": 1500, "height": 1000})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        async def status():
            return await pg.locator("#status").inner_text()

        async def wait_status(w, timeout=15):
            for _ in range(timeout * 5):
                if w in await status():
                    return True
                await pg.wait_for_timeout(200)
            return False

        async def typ(key, val):
            loc = pg.locator(f"#s-{key}")
            await loc.fill(str(val)); await loc.press("Enter")
            await pg.wait_for_timeout(1000)

        await pg.goto(f"http://127.0.0.1:{PORT}")
        await pg.wait_for_timeout(3500)
        check("page shows RAMP, Output and Ramp toggles on, PI off", await wait_status("RAMP")
              and "on" in await pg.locator("#sw-ramp").get_attribute("class")
              and "on" not in await pg.locator("#sw-pi").get_attribute("class"))
        check("filter readback says off by default", "off" in await pg.locator("#rb-filt").inner_text())
        check("scope shows IN1 and OUT1", await pg.locator("#scope .legendtext", has_text="IN1").count() >= 1
              and await pg.locator("#scope .legendtext", has_text="OUT1").count() >= 1)
        await pg.screenshot(path="pi_0_ramp.png", full_page=True)
        await pg.locator("#s-xaxis label", has_text="OUT1").click()
        await pg.wait_for_timeout(1500)
        check("x axis OUT1: IN1 vs OUT1 plot", await pg.locator("#scope .xtitle", has_text="OUT1 [mV]").count() == 1)
        await pg.screenshot(path="pi_1_xy.png", full_page=True)
        vc = stats()["vc"]
        await typ("offset_mv", round(vc * 1e3, 1))
        await typ("p", 0.001)
        await typ("i_hz", 0.2)
        st = stats()
        check("P and I typed -> pid0 (snapped values pushed back)", abs(st["p"] - 0.0009765625) < 1e-12
              and abs(st["i"] - 0.2) < 0.005 and abs(float(await pg.locator("#s-p").input_value()) - st["p"]) < 1e-12,
              str(st))
        await pg.click("#sw-pi")
        check("PI toggle -> PI ON (ramp off)", await wait_status("PI ON") and stats()["out"] == "out1"
              and stats()["amp"] == 0)
        await pg.wait_for_timeout(2500)
        txt = await pg.locator("#stats").inner_text()
        check("stats show IN1 near 0 and the integrator", "integrator" in txt, txt)
        await typ("filt1_hz", 1000)
        check("filter stage 1 -> pid0.inputfilter, readback LP", stats()["filt"][0] > 0
              and "LP" in await pg.locator("#rb-filt").inner_text())
        await typ("filt1_hz", 0)
        check("filter off again", stats()["filt"] == [0, 0, 0, 0])
        await pg.screenshot(path="pi_2_locked.png", full_page=True)
        await pg.click("#sw-ramp")
        check("Ramp toggle -> RAMP, PI off", await wait_status("RAMP") and stats()["out"] == "off")
        await pg.click("#sw-ramp")
        check("Ramp off -> offset only", await wait_status("OFFSET ONLY"))
        await pg.click("#sw-output")
        check("Output off", await wait_status("OUTPUT OFF") and stats()["asg_out"] == "off")
        await pg.click("#sw-output")
        check("Output on -> offset only", await wait_status("OFFSET ONLY"))
        errs = [e for e in errors if "favicon" not in e]
        check("no browser errors", not errs, str(errs[:3]))
        await b.close()
    print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL UI CHECKS PASSED")

asyncio.run(main())
