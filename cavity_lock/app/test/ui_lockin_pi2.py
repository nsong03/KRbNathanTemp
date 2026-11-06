"""Browser test of lockin_pi2_app (v2, Playwright). Start `python serve_lockin_pi2.py` first (fake, port 8096)."""
import asyncio, json, os, urllib.request
from playwright.async_api import async_playwright
PORT = 8096
LAUNCH = dict(executable_path=os.environ.get("CHROME", "/opt/pw-browsers/chromium"), args=["--no-sandbox"]) \
    if os.path.exists(os.environ.get("CHROME", "/opt/pw-browsers/chromium")) else {}
FAILS = []
LOCK_KEYS = ["ac_hz", "lpf_hz", "lpf_order", "lia_gain", "phase_deg", "dither_hz", "dither_mvpp", "filt1_hz",
             "filt2_hz", "filt3_hz", "filt4_hz", "pi_input", "p", "i", "setpoint_mv", "clamp_lo_mv", "clamp_hi_mv",
             "offset_mv", "ramp_hz", "ramp_mvpp", "ramp_shape", "probe_a", "probe_b", "ramp_periods", "live_ms",
             "xaxis", "probe_every", "ui_hz"]
INT_KEYS = ["int_input", "int_filt1_hz", "int_filt2_hz", "int_filt3_hz", "int_filt4_hz", "int_p", "int_i",
            "int_setpoint_mv", "int_clamp_lo_mv", "int_clamp_hi_mv", "int_live_ms"]


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
        await pg.wait_for_timeout(4500)
        check("view icons with status dots", await pg.locator("#vbtn-cav").count() == 1
              and "busy" in await pg.locator("#dot-cav").get_attribute("class"))
        vis = [k for k in LOCK_KEYS if not await pg.locator(f"#s-{k}").is_visible()]
        check("lock view: every parameter and scope setting visible (no popover)", not vis, str(vis))
        for name in ("X", "Y", "R", "IN1", "OUT1"):
            check(f"lock scope trace {name}", await pg.locator("#scope .legendtext", has_text=name).count() >= 1)
        xt = await pg.locator("#scope .xtitle").text_content()
        check("ramp display: 2 ramp periods, triggered on the ramp", "2.0 ramp periods" in xt, xt)
        st = await pg.locator("#status").inner_text()
        f = float(st.split(" traces/s")[0].split()[-1])
        check("lock traces update (~3/s for 2 periods at 20 Hz on the fake)", f > 2.3, f"{f:.1f}/s")
        await pg.screenshot(path="lockin_pi2_0_ramp.png", full_page=True)
        await pg.locator(pid(type="probe", p="pid0")).click(); await pg.wait_for_timeout(2500)
        check("probe dot -> probe A row", stats()["pa"] == "pid0" and
              await pg.locator("#scope .legendtext", has_text="A: pid0").count() == 1
              and await pg.locator("#pb-pid0").inner_text() == "A")
        await pg.locator("#s-xaxis label", has_text="OUT1").click(); await pg.wait_for_timeout(2000)
        check("x axis OUT1", "OUT1 [mV]" in await pg.locator("#scope .xtitle").text_content())
        await pg.locator("#s-xaxis label", has_text="time").click()
        await typ("ramp_periods", 3)
        await pg.wait_for_timeout(2500)
        check("ramp periods 3", "3.0 ramp periods" in await pg.locator("#scope .xtitle").text_content())
        await typ("ramp_periods", 2)
        await typ("phase_deg", 35)
        await typ("offset_mv", round(stats()["vc"] * 1e3, 1))
        print("--- P / I: PyRPL PID widget arrows")
        up = lambda k: pg.locator(f".pyspin-btn[data-key='{k}'][data-dir='1']")
        dn = lambda k: pg.locator(f".pyspin-btn[data-key='{k}'][data-dir='-1']")
        check("P and I have the arrows and %.4e boxes", await up("p").count() == 1 and await dn("i").count() == 1
              and await pg.locator("#s-p").input_value() == "0.0000e+00")
        await up("p").click(); await pg.wait_for_timeout(700)
        check("P: one click = one register step (2^-12)", abs(stats()["p"] - 2 ** -12) < 1e-12
              and await pg.locator("#s-p").input_value() == "2.4414e-04", str(stats()["p"]))
        await up("i").click(); await pg.wait_for_timeout(700)
        check("I: one click = one register step (1/(2^32 2pi 8ns) = 0.004632)",
              abs(stats()["i"] - 1 / (2 ** 32 * 2 * 3.141592653589793 * 8e-9)) < 1e-12
              and await pg.locator("#s-i").input_value() == "4.6320e-03", str(stats()["i"]))
        await typ("p", "1e-3")
        box = await up("p").bounding_box()
        await pg.mouse.move(box["x"] + 5, box["y"] + 5); await pg.mouse.down()
        await pg.wait_for_timeout(1100); await pg.mouse.up(); await pg.wait_for_timeout(700)
        v = stats()["p"]
        check("P held 1.1 s: doubles every 0.5 s (x ~4.6)", 3.2e-3 < v < 6.5e-3, f"{v:.4e}")
        v_txt = float(await pg.locator("#s-p").input_value())
        check("box shows what was written", abs(v_txt - v) < 2.5e-4, f"{v_txt:.4e} vs {v:.4e}")
        await typ("p", "2.4414e-04")
        await dn("p").click(); await pg.wait_for_timeout(700)
        await dn("p").click(); await pg.wait_for_timeout(700)
        check("P down through zero: stops at 0, then negative steps", abs(stats()["p"] + 2 ** -12) < 1e-12,
              str(stats()["p"]))
        await pg.locator("#s-p").focus()
        await pg.keyboard.down("ArrowUp"); await pg.keyboard.up("ArrowUp"); await pg.wait_for_timeout(700)
        check("Up key in the box steps like the button", abs(stats()["p"]) < 1e-12, str(stats()["p"]))
        await typ("p", 0.02)
        await typ("i", 2)
        check("typed values go straight to pid0.p / pid0.i", abs(stats()["p"] - 0.02) < 2.5e-4
              and abs(stats()["i"] - 2) < 0.005)
        await flip("pi")
        check("PI switch -> PI ON", await wait_text("#status", "LOCK: PI ON") and stats()["out"] == "out1")
        await pg.wait_for_timeout(2500)
        xt = await pg.locator("#scope .xtitle").text_content()
        check("PI on: live traces", "live" in xt, xt)
        await pg.screenshot(path="lockin_pi2_1_locked.png", full_page=True)
        await pg.click("#vbtn-int"); await pg.wait_for_timeout(1000)
        vis = [k for k in INT_KEYS if not await pg.locator(f"#s-{k}").is_visible()]
        check("Intensity view: every parameter and its scope setting visible", not vis
              and await pg.locator("#iscope").is_visible() and not await pg.locator("#scope").is_visible(), str(vis))
        await pg.locator(".pyspin-btn[data-key='int_i'][data-dir='1']").click(); await pg.wait_for_timeout(700)
        check("intensity I arrow: one register step", abs(stats()["ii"] - 0.0046320) < 1e-6, str(stats()["ii"]))
        await typ("int_i", "3.8e4")
        i0 = stats()["ii"]
        await pg.locator(".pyspin-btn[data-key='int_i'][data-dir='1']").click(); await pg.wait_for_timeout(700)
        check("single step at a large I is not lost: the register moves by one step, display %.4e",
              abs(stats()["ii"] - i0 - 0.0046320) < 1e-6
              and await pg.locator("#s-int_i").input_value() == "3.8000e+04", f"{i0:.6f} -> {stats()['ii']:.6f}")
        await typ("int_p", 0.01)
        await typ("int_i", 2000)
        await typ("int_setpoint_mv", 400)
        await flip("int_on")
        await pg.wait_for_timeout(2000)
        txt = await pg.locator("#istats").inner_text()
        check("intensity servo on, scope shows IN2 at the setpoint", stats()["iout"] == "out2"
              and ("IN2 mean +4" in txt or "IN2 mean +39" in txt), txt)
        st = await pg.locator("#status").inner_text()
        f = float(st.split(" traces/s")[0].split()[-1])
        check("intensity view: intensity traces fast", f > 8, f"{f:.1f}/s")
        await pg.screenshot(path="lockin_pi2_2_intensity.png", full_page=True)
        await pg.click("#vbtn-cav"); await pg.wait_for_timeout(800)
        check("PI → OUT2 switch on the lock diagram, off", await pg.locator(pid(type="sw", s="pi_out2")).count() == 1
              and not await on("pi_out2"))
        await flip("pi_out2")
        st = stats()
        check("PI → OUT2 on: pid0 -> out2, intensity servo off", await on("pi_out2") and st["out"] == "out2"
              and st["iout"] == "off" and await wait_text("#status", "SERVO OFF"), str(st))
        await pg.screenshot(path="lockin_pi2_3_pi_out2.png", full_page=True)
        await flip("pi_out2")
        check("PI → OUT2 off: pid0 back to out1", stats()["out"] == "out1" and not await on("pi_out2"))
        await flip("ramp")
        check("Ramp switch -> RAMP", await wait_text("#status", "LOCK: RAMP") and stats()["out"] == "off")
        await flip("output")
        check("lock Output off", await wait_text("#status", "OUTPUT OFF"))
        errs = [e for e in errors if "favicon" not in e]
        check("no browser errors", not errs, str(errs[:3]))
        await b.close()
    print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL UI CHECKS PASSED")

asyncio.run(main())
