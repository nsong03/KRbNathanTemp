"""Browser test of errsig2ph_app (Playwright). Start `python serve_errsig2ph.py` first (fake, port 8066)."""
import asyncio, json, os, re, sys, urllib.request
from playwright.async_api import async_playwright
PORT = 8066
LAUNCH = dict(executable_path=os.environ.get("CHROME", "/opt/pw-browsers/chromium"), args=["--no-sandbox"]) \
    if os.path.exists(os.environ.get("CHROME", "/opt/pw-browsers/chromium")) else {}
FAILS = []


def check(name, ok, info=""):
    print(f"   [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({info})" if info else ""), flush=True)
    if not ok:
        FAILS.append(name)


def pid(**kw):
    return "[id='" + json.dumps(kw, sort_keys=True, separators=(",", ":")) + "']"


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

        async def wait_status(w, timeout=20):
            for _ in range(timeout * 5):
                if w in await status():
                    return True
                await pg.wait_for_timeout(200)
            return False

        async def flip(name):
            await pg.locator(pid(type="sw", s=name)).click()
            await pg.wait_for_timeout(900)

        await pg.goto(f"http://127.0.0.1:{PORT}")
        await pg.wait_for_timeout(4000)
        check("page shows RAMP (alignment) with all switches", await wait_status("RAMP") and
              await pg.locator(".sw").count() == 4)
        check("no PID / Lock controls on the page", await pg.locator(pid(type="sw", s="lock")).count() == 0
              and await pg.locator(".b-pid").count() == 0 and await pg.locator(".b-limiter").count() == 0)
        await pg.screenshot(path="errsig2ph_0_scan.png", full_page=True)
        await pg.locator("#s-phase_deg").fill("80"); await pg.locator("#s-phase_deg").press("Enter")
        await pg.wait_for_timeout(2500)
        tt = await pg.locator("#target-text").inner_text()
        check("target text shows theta from one sweep", "θ" in tt, tt)
        check("scope shows X, Y and R traces", await pg.locator("#scope .legendtext", has_text="Y").count() >= 1
              and await pg.locator("#scope .legendtext", has_text="R").count() >= 1
              and await pg.locator("#scope .legendtext", has_text="X (error)").count() >= 1)
        check("diagram: two mixers (X, Y) and the iq2_2 probe", await pg.locator(".b-mixerq, [id*='mixerq']").count() >= 1
              and await pg.locator(pid(type="probe", p="iq2_2")).count() == 1)
        await pg.click("#btn-phase")
        await pg.wait_for_timeout(6000)
        log = await pg.locator("#log").inner_text()
        check("Auto phase ran (theta -> 0)", "auto phase done: theta" in log)
        st = stats()
        check("theta near 0 after Auto", abs(st["xy"].get("theta", 99)) < 3, str(st["xy"]))
        await pg.locator("#s-lpf_order label", has_text="4").click()
        await pg.wait_for_timeout(1200)
        check("low-pass order radio goes to 4", "lpf_order=4" in await pg.locator("#log").inner_text())
        await pg.locator("#s-eo_source label", has_text="Y").click()
        await pg.wait_for_timeout(1200)
        check("error output source radio Y -> pid1 input iq2_2", stats()["pid1_in"] == "iq2_2")
        await pg.locator("#s-eo_source label", has_text="X").click()
        await pg.wait_for_timeout(1200)
        check("error output source radio X -> pid1 input iq2", stats()["pid1_in"] == "iq2")
        await pg.click("#btn-park")
        check("Park at peak -> SERVO MODE", await wait_status("SERVO MODE", 10))
        st = stats()
        check("OUT2 = DC offset at the setpoint + IN2 correction (pid0 on, i = 0)", st["amp"] == 0
              and abs(st["offset"] * 1e3 - st["center"]) < 1e-6 and st["pid0_out"] == "out2" and st["pid0_i"] == 0,
              str(st))
        rb = await pg.locator("#rb-errout").inner_text()
        check("Error output box shows the error slope", "slope" in rb, rb.replace("\n", " | "))
        await pg.locator(".b-ramp .spadj button", has_text=re.compile(r"^\+1$")).click()
        await pg.wait_for_timeout(1000)
        st2 = stats()
        check("+1 mV step moves the OUT2 DC offset", abs(st2["offset"] - st["offset"] - 0.001) < 1e-9, str(st2))
        await pg.wait_for_timeout(1500)
        await pg.screenshot(path="errsig2ph_1_servo.png", full_page=True)
        loc = pg.locator("#s-in2_gain")
        await loc.fill("0.8"); await loc.press("Enter")
        await pg.wait_for_timeout(1200)
        check("correction gain 0.8 applied", "in2_gain=0.8" in await pg.locator("#log").inner_text())
        loc = pg.locator("#s-eo_gain")
        await loc.fill("-2"); await loc.press("Enter")
        await pg.wait_for_timeout(1200)
        check("error output gain -2 applied", "eo_gain=-2" in await pg.locator("#log").inner_text())
        await flip("errout")
        check("Error out off", stats()["pid1_out"] == "off" and "off" in await status())
        await flip("errout")
        await flip("ramp")
        check("Ramp toggle on: RAMP, correction off", await wait_status("RAMP", 10) and stats()["pid0_out"] == "off")
        await pg.screenshot(path="errsig2ph_2_ramp.png", full_page=True)
        await flip("ramp")
        check("Ramp toggle off: SERVO MODE", await wait_status("SERVO MODE", 10) and stats()["pid0_out"] == "out2")
        await flip("output")
        check("Output off", await wait_status("OUTPUT OFF", 10))
        await flip("output")
        check("Output on -> ramp", await wait_status("RAMP", 10))
        check("pid0 / pid1 never integrate", stats()["pid1_i"] == 0 and stats()["pid0_i"] == 0)
        errs = [e for e in errors if "favicon" not in e]
        check("no browser errors", not errs, str(errs[:3]))
        await b.close()
    print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL UI CHECKS PASSED")

asyncio.run(main())
