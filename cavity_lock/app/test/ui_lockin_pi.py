"""Browser test of lockin_pi_app (Playwright). Start `python serve_lockin_pi.py` first (fake, port 8086)."""
import asyncio, json, os, urllib.request
from playwright.async_api import async_playwright
PORT = 8086
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
        pg = await b.new_page(viewport={"width": 1500, "height": 1100})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        async def wait_text(sel, w, timeout=15):
            for _ in range(timeout * 5):
                if w in await pg.locator(sel).inner_text():
                    return True
                await pg.wait_for_timeout(200)
            return False

        async def typ(key, val):
            loc = pg.locator(f"#s-{key}")
            await loc.fill(str(val)); await loc.press("Enter")
            await pg.wait_for_timeout(900)

        async def on(sid):
            return "on" in (await pg.locator(f"#sw-{sid}").get_attribute("class") or "").split()

        async def click(sid):
            await pg.click(f"#sw-{sid}")
            await pg.wait_for_timeout(900)

        await pg.goto(f"http://127.0.0.1:{PORT}")
        await pg.wait_for_timeout(3500)
        check("lock RAMP, Output/Ramp/Dither on, PI/Filter off", await wait_text("#status", "RAMP")
              and await on("output") and await on("ramp") and await on("dither") and not await on("pi")
              and not await on("filt"))
        check("intensity SERVO OFF, Servo/Filter toggles off", await wait_text("#istatus", "SERVO OFF")
              and not await on("int_on") and not await on("int_filt"))
        for name in ("X", "Y", "R", "IN1", "OUT1"):
            check(f"lock scope trace {name}", await pg.locator("#scope .legendtext", has_text=name).count() >= 1)
        check("intensity scope IN2 and OUT2", await pg.locator("#iscope .legendtext", has_text="IN2").count() >= 1
              and await pg.locator("#iscope .legendtext", has_text="OUT2").count() >= 1)
        await typ("ramp_hz", 5)
        await typ("phase_deg", 35)
        check("phase typed -> iq2.phase", abs(stats()["phase"] - 35) < 1e-9)
        await pg.wait_for_timeout(1500)
        await pg.screenshot(path="lockin_pi_0_ramp.png", full_page=True)
        await pg.locator("#s-xaxis label", has_text="OUT1").click()
        await pg.wait_for_timeout(1500)
        check("x axis OUT1", await pg.locator("#scope .xtitle", has_text="OUT1 [mV]").count() == 1)
        await pg.locator("#s-xaxis label", has_text="time").click()
        await typ("offset_mv", round(stats()["vc"] * 1e3, 1))
        await typ("p", 0.02)
        await typ("i_hz", 2)
        st = stats()
        check("raw P and I typed -> pid0 (snapped)", abs(st["p"] - 0.02) < 2.5e-4 and abs(st["i"] - 2) < 0.005, str(st))
        await click("pi")
        check("PI toggle -> PI ON, ramp off", await wait_text("#status", "PI ON") and stats()["out"] == "out1"
              and stats()["amp"] == 0 and not await on("ramp"))
        await pg.wait_for_timeout(2000)
        check("lock stats show the integrator", "integrator" in await pg.locator("#stats").inner_text())
        await typ("filt1_hz", 1000)
        check("filter value alone: pid0 filter still off", stats()["filt"] == [0, 0, 0, 0])
        await click("filt")
        check("Filter toggle -> pid0 filter on", await on("filt") and stats()["filt"][0] > 0)
        await click("filt")
        check("Filter toggle off", stats()["filt"] == [0, 0, 0, 0])
        await pg.locator("#s-pi_input label", has_text="Y").click(); await pg.wait_for_timeout(900)
        check("PI input Y", stats()["pin"] == "iq2_2")
        await pg.locator("#s-pi_input label", has_text="X").click(); await pg.wait_for_timeout(900)
        await typ("int_p", 0.01)
        await typ("int_i_hz", 2000)
        await typ("int_setpoint_mv", 400)
        await click("int_on")
        st = stats()
        check("intensity Servo toggle -> pid2 on OUT2 with the raw gains", await wait_text("#istatus", "SERVO ON")
              and st["iout"] == "out2" and abs(st["ip"] - 0.01) < 2.5e-4 and abs(st["ii"] - 2000) < 1, str(st))
        await pg.wait_for_timeout(1500)
        txt = await pg.locator("#istats").inner_text()
        check("intensity stats: IN2 near the setpoint", "IN2 mean +4" in txt or "IN2 mean +39" in txt, txt)
        await typ("int_filt1_hz", 20000)
        await click("int_filt")
        check("intensity Filter toggle", stats()["ifilt"][0] > 0)
        await click("int_filt")
        await pg.screenshot(path="lockin_pi_1_locked.png", full_page=True)
        await click("dither")
        check("Dither toggle off", stats()["dither"] == "off" and not await on("dither"))
        await click("dither")
        await click("ramp")
        check("Ramp toggle -> RAMP, PI off", await wait_text("#status", "RAMP") and stats()["out"] == "off")
        await click("output")
        check("lock Output off", await wait_text("#status", "OUTPUT OFF") and stats()["iout"] == "out2")
        await click("int_on")
        check("intensity Servo off", await wait_text("#istatus", "SERVO OFF") and stats()["iout"] == "off")
        errs = [e for e in errors if "favicon" not in e]
        check("no browser errors", not errs, str(errs[:3]))
        await b.close()
    print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL UI CHECKS PASSED")

asyncio.run(main())
