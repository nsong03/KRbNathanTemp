"""Browser test of lockin_pi_app (Playwright). Start `python serve_lockin_pi.py` first (fake, port 8086)."""
import asyncio, json, os, urllib.request
from playwright.async_api import async_playwright
PORT = 8086
LAUNCH = dict(executable_path=os.environ.get("CHROME", "/opt/pw-browsers/chromium"), args=["--no-sandbox"]) \
    if os.path.exists(os.environ.get("CHROME", "/opt/pw-browsers/chromium")) else {}
FAILS = []
SCOPE_KEYS = ["sc_ch1", "sc_ch2", "sc_duration", "sc_trigger", "sc_threshold_mv", "sc_hysteresis_mv", "sc_delay_ms",
              "sc_average", "sc_rolling", "sc_view", "sc_math", "sc_ch1_min_mv", "sc_ch1_max_mv", "sc_ch2_min_mv",
              "sc_ch2_max_mv", "ui_hz"]
LOCK_KEYS = ["ac_hz", "lpf_hz", "lpf_order", "lia_gain", "phase_deg", "dither_hz", "dither_mvpp", "filt1_hz",
             "filt2_hz", "filt3_hz", "filt4_hz", "pi_input", "p", "i", "setpoint_mv", "clamp_lo_mv", "clamp_hi_mv",
             "offset_mv", "ramp_hz", "ramp_mvpp", "ramp_shape"]
INT_KEYS = ["int_input", "int_filt1_hz", "int_filt2_hz", "int_filt3_hz", "int_filt4_hz", "int_p", "int_i",
            "int_setpoint_mv", "int_clamp_lo_mv", "int_clamp_hi_mv"]


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

        async def pick(key, label):
            await pg.locator(f"#s-{key}").click()
            await pg.wait_for_timeout(300)
            await pg.get_by_role("option", name=label, exact=True).first.click()
            await pg.wait_for_timeout(1200)

        async def on(sid, P=""):
            return "on" in (await pg.locator(f"#{P}swp-{sid}").get_attribute("class") or "").split()

        async def flip(sid):
            await pg.locator(pid(type="sw", s=sid)).click()
            await pg.wait_for_timeout(1000)

        async def fps():
            t = await pg.locator("#scopestat").inner_text()
            return float(t.split(" traces/s")[0])

        await pg.goto(f"http://127.0.0.1:{PORT}")
        await pg.wait_for_timeout(4000)
        check("two view icons (Lock, Intensity) with status dots, like the cavity app",
              await pg.locator("#vbtn-cav").count() == 1 and await pg.locator("#vbtn-int").count() == 1
              and "busy" in await pg.locator("#dot-cav").get_attribute("class")
              and "off" in await pg.locator("#dot-int").get_attribute("class"))
        check("lock view: 5 switches, wires drawn", await pg.locator("#view-cavity .sw").count() == 5
              and await pg.locator("#wires path").count() > 15 and await pg.locator("#view-cavity").is_visible())
        vis = [k for k in LOCK_KEYS if not await pg.locator(f"#s-{k}").is_visible()]
        check("every lock parameter visible in its box (nothing behind a popover)", not vis, str(vis))
        vis = [k for k in SCOPE_KEYS if not await pg.locator(f"#s-{k}").is_visible()]
        check("every scope setting visible in the scope panel", not vis, str(vis))
        check("lock RAMP, Output/Ramp/Dither on, PI/Filter off", await wait_text("#status", "LOCK: RAMP")
              and await on("output") and await on("ramp") and await on("dither") and not await on("pi")
              and not await on("filt"))
        await pg.screenshot(path="lockin_pi_0_ramp.png", full_page=True)
        await pg.click("#vbtn-int"); await pg.wait_for_timeout(800)
        vis = [k for k in INT_KEYS if not await pg.locator(f"#s-{k}").is_visible()]
        check("Intensity view: its canvas, every parameter visible", await pg.locator("#view-intensity").is_visible()
              and not await pg.locator("#view-cavity").is_visible() and not vis
              and await pg.locator("#i-wires path").count() >= 5, str(vis))
        await typ("int_p", 0.01)
        await typ("int_i", 2000)
        await typ("int_setpoint_mv", 400)
        await flip("int_on")
        st = stats()
        check("Servo switch -> pid2 on OUT2 with the typed gains, dot green", await wait_text("#status", "SERVO ON")
              and st["iout"] == "out2" and abs(st["ip"] - 0.01) < 2.5e-4 and abs(st["ii"] - 2000) < 1
              and "ok" in await pg.locator("#dot-int").get_attribute("class"), str(st))
        await pg.locator(pid(type="probe", p="in2")).click(); await pg.wait_for_timeout(1500)
        check("intensity probe in2 -> a scope channel", "in2" in (stats()["ch1"], stats()["ch2"]))
        await pg.screenshot(path="lockin_pi_2_intensity.png", full_page=True)
        await pg.click("#vbtn-cav"); await pg.wait_for_timeout(800)
        await typ("phase_deg", 35)
        await typ("offset_mv", round(stats()["vc"] * 1e3, 1))
        await typ("p", 0.02)
        await typ("i", 2)
        st = stats()
        check("P and I typed -> pid0.p / pid0.i", abs(st["p"] - 0.02) < 2.5e-4 and abs(st["i"] - 2) < 0.005, str(st))
        await pg.wait_for_timeout(1500)
        check("boxes keep the typed values (no rewrite)", await pg.locator("#s-p").input_value() == "0.02"
              and await pg.locator("#s-i").input_value() == "2")
        rb = await pg.locator("#rb-pid").inner_text()
        check("PI readback shows the hardware values", "hw p +0.0200195" in rb and "i +2.00103" in rb,
              rb.replace("\n", " | "))
        await flip("pi")
        check("PI switch -> PI ON, ramp off", await wait_text("#status", "LOCK: PI ON") and stats()["out"] == "out1"
              and stats()["amp"] == 0 and not await on("ramp"))
        await typ("filt1_hz", 1000)
        check("filter value alone: pid0 filter still off", stats()["filt"] == [0, 0, 0, 0])
        await flip("filt")
        check("Filter switch -> pid0 filter on", await on("filt") and stats()["filt"][0] > 0)
        await flip("filt")
        print("--- scope panel")
        await pick("sc_ch1", "iq2_2")
        await pick("sc_ch2", "out1")
        check("CH1 / CH2 dropdowns -> scope inputs", stats()["ch1"] == "iq2_2" and stats()["ch2"] == "out1",
              str(stats()))
        await pick("sc_trigger", "immediately")
        await pick("sc_duration", "33.6 ms")
        await pg.wait_for_timeout(2500)
        f = await fps()
        check("free running 33.6 ms: live (> 8 traces/s)", f > 8 and abs(stats()["dur"] - 0.0335544) < 1e-6
              and stats()["trig"] == "immediately", f"{f:.1f}/s")
        await typ("sc_ch2_min_mv", -100)
        await typ("sc_ch2_max_mv", 100)
        await pg.wait_for_timeout(800)
        rng = await pg.evaluate("() => document.getElementById('scope').querySelector('.js-plotly-plot')"
                                ".layout.yaxis2.range")
        check("CH2 y range set by hand", rng == [-100, 100], str(rng))
        await pg.locator("#s-sc_view label", has_text="XY").click(); await pg.wait_for_timeout(1500)
        xt = await pg.locator("#scope .xtitle").text_content()
        check("XY display", xt == "CH1 iq2_2 [mV]", xt)
        await pg.locator("#s-sc_view label", has_text="time").click()
        await pg.locator("#s-sc_math label", has_text="R").click(); await pg.wait_for_timeout(1500)
        check("math R row", await pg.locator("#scope .legendtext", has_text="R = sqrt").count() == 1)
        await pg.locator("#s-sc_math label", has_text="none").click()
        await pick("sc_duration", "1.07 s")
        await pg.locator("#s-sc_rolling label", has_text="on").click()
        await pg.wait_for_timeout(2500)
        f = await fps()
        xt = await pg.locator("#scope .xtitle").text_content()
        check("rolling mode at 1.07 s: live at the browser rate", f > 7 and "rolling" in xt, f"{f:.1f}/s, {xt}")
        await pg.locator("#s-sc_rolling label", has_text="off").click()
        await pick("sc_trigger", "ext_positive_edge")
        await pg.wait_for_timeout(3000)
        check("no trigger: WAITING FOR TRIGGER", "WAITING FOR TRIGGER" in await pg.locator("#scopestat").inner_text())
        await pick("sc_trigger", "asg0")
        await pick("sc_duration", "268 ms")
        await pg.click("#btn-run"); await pg.wait_for_timeout(1000)
        check("RUN button -> STOPPED", await pg.locator("#btn-run").inner_text() == "STOPPED")
        await pg.click("#btn-run"); await pg.wait_for_timeout(1000)
        await pg.locator(pid(type="probe", p="iq2")).click(); await pg.wait_for_timeout(500)
        await pg.locator(pid(type="probe", p="pid0")).click(); await pg.wait_for_timeout(1500)
        st = stats()
        check("probe dots put signals on CH1 / CH2 (badges 1 / 2)", {st["ch1"], st["ch2"]} == {"iq2", "pid0"}
              and await pg.locator("#pb-iq2").inner_text() in ("1", "2"), str(st))
        await pg.wait_for_timeout(1500)
        await pg.screenshot(path="lockin_pi_1_locked.png", full_page=True)
        await flip("ramp")
        check("Ramp switch -> RAMP, PI off", await wait_text("#status", "LOCK: RAMP") and stats()["out"] == "off")
        before = float(await pg.locator("#s-offset_mv").input_value())
        await pg.locator(".b-ramp .spadj button", has_text="+10").click(); await pg.wait_for_timeout(1500)
        after = float(await pg.locator("#s-offset_mv").input_value())
        check("offset +10 mV button", abs(after - before - 10) < 1e-6, f"{before} -> {after}")
        await flip("output")
        check("lock Output off", await wait_text("#status", "OUTPUT OFF") and stats()["iout"] == "out2")
        errs = [e for e in errors if "favicon" not in e]
        check("no browser errors", not errs, str(errs[:3]))
        await b.close()
    print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL UI CHECKS PASSED")

asyncio.run(main())
