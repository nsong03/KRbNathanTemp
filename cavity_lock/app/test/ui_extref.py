"""Browser test of extref_app (Playwright). Start `python serve_extref.py` first (fake, port 8106)."""
import asyncio, json, os, urllib.request
from playwright.async_api import async_playwright
PORT = 8106
LAUNCH = dict(executable_path=os.environ.get("CHROME", "/opt/pw-browsers/chromium"), args=["--no-sandbox"]) \
    if os.path.exists(os.environ.get("CHROME", "/opt/pw-browsers/chromium")) else {}
FAILS = []
KEYS = ["ref_hz", "ref_lpf_hz", "in2_scale", "phase_deg", "lpf_hz", "lpf_order", "ac_hz", "lia_gain", "pi_input",
        "p", "i", "setpoint_mv", "clamp_lo_mv", "clamp_hi_mv", "filt1_hz", "filt2_hz", "filt3_hz", "filt4_hz",
        "eo_source", "eo_gain", "eo_offset_mv", "eo_limit_mv", "offset_mv", "ramp_hz", "ramp_mvpp", "ramp_shape",
        "live_ms", "xaxis", "ramp_periods", "ui_hz", "probe_every", "probe_a", "probe_b"]


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
            return "on" in (await pg.locator(f"#swp-{sid}").get_attribute("class") or "").split()

        async def flip(sid):
            await pg.locator(pid(type="sw", s=sid)).click()
            await pg.wait_for_timeout(1000)

        await pg.goto(f"http://127.0.0.1:{PORT}")
        await pg.wait_for_timeout(5000)
        vis = [k for k in KEYS if not await pg.locator(f"#s-{k}").is_visible()]
        check("every parameter visible (inline)", not vis, str(vis))
        check("7 switches, wires drawn, no dither block", await pg.locator(".sw").count() == 7
              and await pg.locator("#wires path").count() > 20 and await pg.locator(".b-dither").count() == 0)
        check("reference tracked: REF: LOCKED", await wait_text("#status", "REF: LOCKED", 20))
        rb = await pg.locator("#rb-reference").inner_text()
        check("reference box: NCO, LOCKED, drift", "NCO 1000.0000" in rb and "LOCKED" in rb, rb.replace("\n", " | "))
        for name in ("X", "Y", "R", "IN1", "OUT1"):
            check(f"scope trace {name}", await pg.locator("#scope .legendtext", has_text=name).count() >= 1)
        xt = await pg.locator("#scope .xtitle").text_content()
        check("ramp display: 2 ramp periods", "2.0 ramp periods" in xt, xt)
        await pg.screenshot(path="extref_0_ramp.png", full_page=True)
        await pg.click("#btn-find"); await pg.wait_for_timeout(2500)
        st = stats()
        check("Find f -> NCO = the IN2 frequency", abs(st["f"] - st["f_ext"]) < 0.005, f"{st['f']:.4f}")
        check("reference box shows the IN2 measurement", "IN2: 1000.01" in await pg.locator("#rb-reference").inner_text())
        await flip("track")
        check("Track off", not await on("track") and await wait_text("#status", "TRACKING OFF"))
        await flip("track")
        await pg.locator(".pyspin-btn[data-key='p'][data-dir='1']").click(); await pg.wait_for_timeout(700)
        check("P arrow: one register step", abs(stats()["p"] - 2 ** -12) < 1e-12)
        await typ("clamp_lo_mv", -300)
        await typ("clamp_hi_mv", 300)
        check("PI limiters", abs(stats()["lo"] + 0.3) < 1e-9 and abs(stats()["hi"] - 0.3) < 1e-9)
        await flip("eo")
        check("Error → OUT2: pid1 on out2", stats()["eo"] == "out2" and await on("eo")
              and await wait_text("#status", "OUT2: ERROR"))
        await pg.locator("#s-eo_source label", has_text="Y").click(); await pg.wait_for_timeout(900)
        check("error source Y", stats()["eo_in"] == "iq2_2")
        await flip("pi_out2")
        st = stats()
        check("PI → OUT2 turns Error → OUT2 off", st["eo"] == "off" and st["out"] == "out2" and not await on("eo"))
        await flip("pi")
        check("PI → OUT1: ramp off, PI on both", stats()["out"] == "both" and stats()["amp"] == 0
              and not await on("ramp"))
        await pg.wait_for_timeout(1500)
        await pg.screenshot(path="extref_1_pi.png", full_page=True)
        await flip("ramp")
        check("Ramp on: PI off OUT1 (still on OUT2)", stats()["out"] == "out2" and stats()["amp"] > 0)
        await flip("output")
        check("Output off", await wait_text("#status", "OUT1: OFF"))
        errs = [e for e in errors if "favicon" not in e]
        check("no browser errors", not errs, str(errs[:3]))
        await b.close()
    print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL UI CHECKS PASSED")

asyncio.run(main())
