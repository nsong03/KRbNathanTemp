"""Browser test of the schematic UI (Playwright). Start `python serve_v3.py` first (fake hardware, port 8070).
Edits every block parameter, flips every switch, places probes, targets a peak with a Lock Assist crosshair,
locks with the Lock switch, runs Measure noise and Measure loop, measures redraws per second, saves screenshots."""
import asyncio
import json
import sys

sys.path.insert(0, "..")
import cavity_lock_app as A        # noqa: E402  (FIELDS; no hardware is touched)
import schematic as SCH            # noqa: E402
from playwright.async_api import async_playwright   # noqa: E402

PORT = 8070
FIELD = {f[0]: f for f in A.FIELDS}
NEW = dict(ramp_hz="13", ramp_mvpp="300", ramp_shape="sawtooth", ramp_center_mv="30", acq_dither_hz="1700",
           acq_dither_mvpp="20", phase_deg="80", ac_hz="300", lpf_hz="600", lpf_order=1, err_gain="3",
           setpoint_mv="2", out2_signal="photodiode", out2_gain="2", ugf_hz="15", p_gain="0.3", clamp_auto=None,
           clamp_mv="30", lock_same_dither=None, lock_dither_hz="2500", lock_dither_mvpp="6", in1_scale="2",
           kappa_mhz="35", driver_bw_hz="800")
FAILS = []


def check(name, ok, info=""):
    print(f"   [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({info})" if info else ""), flush=True)
    if not ok:
        FAILS.append(name)


def pid(**kw):
    """CSS selector for a Dash pattern-matching id (keys sorted, as Dash renders them)."""
    return "[id='" + json.dumps(kw, sort_keys=True, separators=(",", ":")) + "']"


JS_FPS = """() => new Promise(res => { const gd = document.querySelector('#scope .js-plotly-plot'); let n = 0;
  gd.on('plotly_afterplot', () => n++); setTimeout(() => res(n / 5), 5000); })"""
JS_TRACES = """() => { const gd = document.querySelector('#scope .js-plotly-plot');
  return gd.data.map(d => ({name: d.name, mode: d.mode, x: d.x, y: d.y, sym: d.marker && d.marker.symbol,
                            cd: d.customdata || null, text: d.text || null})); }"""
JS_PIX = """([t, y, ax]) => { const gd = document.querySelector('#scope .js-plotly-plot');
  const xa = gd._fullLayout.xaxis, ya = gd._fullLayout[ax], bb = gd.getBoundingClientRect();
  return [bb.left + xa._offset + xa.l2p(t), bb.top + ya._offset + ya.l2p(y)]; }"""


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1500, "height": 1000})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        dialogs = []

        async def on_dialog(d):
            dialogs.append(d.message)
            await d.accept()
        pg.on("dialog", lambda d: asyncio.ensure_future(on_dialog(d)))

        async def status():
            return await pg.locator("#status").inner_text()

        async def wait_status(word, timeout=40):
            for _ in range(int(timeout * 5)):
                if word in await status():
                    return True
                await pg.wait_for_timeout(200)
            return False

        async def log_text():
            return await pg.locator("#log").inner_text()

        async def pill(name):
            return await pg.locator(f"#swp-{name}").get_attribute("class")

        async def flip(name):
            await pg.locator(pid(type="sw", s=name)).click()
            await pg.wait_for_timeout(900)

        await pg.goto(f"http://127.0.0.1:{PORT}")
        await pg.wait_for_timeout(4000)
        check("page shows SCANNING with every switch drawn", "SCANNING" in await status()
              and all([await pg.locator(f"#swp-{s['id']}").count() == 1 for s in SCH.SWITCHES]))
        fps_acq = await pg.evaluate(JS_FPS)
        print("browser redraws/s while scanning:", fps_acq)
        check("redraws >= 7/s while scanning", fps_acq >= 7, f"{fps_acq}")
        await pg.screenshot(path="v4_0_scan.png")

        # ---- popover ----
        await pg.locator(pid(type="hdr", b="lowpass")).click()
        await pg.wait_for_timeout(400)
        check("clicking a block title opens its popover",
              await pg.locator(pid(type="pop", b="lowpass")).is_visible())
        await pg.screenshot(path="v4_1_popover.png", clip={"x": 0, "y": 40, "width": 1500, "height": 560})
        await pg.locator(pid(type="popx", b="lowpass")).click()
        await pg.wait_for_timeout(300)
        check("x closes it", not await pg.locator(pid(type="pop", b="lowpass")).is_visible())

        # ---- every block parameter, through its own input on the diagram ----
        print("--- editing every block parameter")

        async def set_value(key, val):
            kind = FIELD[key][4]
            loc = pg.locator(f"#s-{key}")
            if kind == "check":
                await loc.locator("input").click()
            elif key == "lpf_order":
                await loc.locator("input").nth(int(val) - 1).click()
            elif kind == "select":
                await loc.click()
                await pg.wait_for_timeout(250)
                await pg.locator(".dash-dropdown-option", has_text=val).first.click()
            else:
                await loc.fill(str(val))
                await loc.press("Enter")
            await pg.wait_for_timeout(700)

        async def get_value(key):
            kind = FIELD[key][4]
            loc = pg.locator(f"#s-{key}")
            if kind == "check":
                return await loc.locator("input").is_checked()
            if key == "lpf_order":
                return 1 if await loc.locator("input").nth(0).is_checked() else 2
            if kind == "select":
                return (await loc.inner_text()).strip()
            return await loc.input_value()

        for blk in SCH.BLOCKS:
            for prm in blk["params"]:
                k = prm["key"]
                if not prm["inline"]:
                    await pg.locator(pid(type="hdr", b=blk["id"])).click()
                    await pg.wait_for_timeout(300)
                old = await get_value(k)
                new = NEW[k] if NEW[k] is not None else None
                if k == "ramp_shape":
                    new = "sawtooth" if old != "sawtooth" else "triangle"
                await set_value(k, new)
                lg = await log_text()
                ok = f"{k}=" in "\n".join(lg.splitlines()[:6])
                rb = await pg.locator(f"#rb-{blk['id']}").inner_text()
                check(f"{blk['id']}: {k} {old} -> {new if new is not None else 'toggled'}", ok,
                      rb.replace("\n", " | ")[:80])
                # restore
                await set_value(k, old)
                if not prm["inline"]:
                    await pg.locator(pid(type="popx", b=blk["id"])).click()
                    await pg.wait_for_timeout(200)
        await pg.wait_for_timeout(1500)

        # ---- switches while scanning ----
        print("--- switches")
        await flip("dither")
        check("Dither switch off -> 'dither OFF', pill off", "dither OFF" in await status()
              and "on" not in (await pill("dither")).split())
        await pg.screenshot(path="v4_2_dither_off.png", clip={"x": 0, "y": 40, "width": 1500, "height": 560})
        await flip("dither")
        check("Dither switch on", "on" in (await pill("dither")).split())
        await flip("scan")
        check("Scan off -> HELD at the ramp centre", await wait_status("HELD", 5) and
              "on" not in (await pill("scan")).split())
        await flip("scan")
        check("Scan on -> SCANNING", await wait_status("SCANNING", 5))
        await flip("output")
        check("Output off -> OUTPUT OFF, wires to OUT1 dead", await wait_status("OUTPUT OFF", 5) and
              "off" in (await pg.locator("#wire-sum_out").get_attribute("class")))
        await pg.screenshot(path="v4_3_output_off.png", clip={"x": 0, "y": 40, "width": 1500, "height": 560})
        await flip("lock")
        await pg.wait_for_timeout(500)
        check("Lock refused while Output is off", "OUTPUT OFF" in await status())
        await flip("output")
        check("Output on -> SCANNING", await wait_status("SCANNING", 5))
        await flip("relock")
        off = "on" not in (await pill("relock")).split()
        await flip("relock")
        check("Auto-relock switch toggles", off and "on" in (await pill("relock")).split())
        await pg.wait_for_timeout(2500)

        # ---- probes ----
        print("--- probes")
        await pg.locator(pid(type="probe", p="iq0")).click()
        await pg.wait_for_timeout(600)
        await pg.locator(pid(type="probe", p="pid0")).click()
        await pg.wait_for_timeout(1500)
        names = [d["name"] for d in await pg.evaluate(JS_TRACES)]
        check("probes A = iq0, B = pid0 reach the scope", "A: iq0" in names and "B: pid0" in names, str(names))
        check("probe badges A/B on the diagram", (await pg.locator("#pb-iq0").inner_text()) == "A"
              and (await pg.locator("#pb-pid0").inner_text()) == "B")
        tip = await pg.locator("#pb-mix").evaluate("e => e.parentElement.title")
        await pg.locator("#pb-mix").click(force=True)
        await pg.wait_for_timeout(500)
        check("grey probe point (raw mixer) has a tooltip and does nothing", "not probe-able" in tip and
              (await pg.locator("#pb-mix").inner_text()) == "", tip[:60])
        await pg.locator(pid(type="probe", p="out2")).click()
        await pg.wait_for_timeout(1500)
        names = [d["name"] for d in await pg.evaluate(JS_TRACES)]
        check("a third probe replaces the older one (A)", "A: out2" in names and "B: pid0" in names, str(names))
        await pg.screenshot(path="v4_4_probes.png")
        for sig in ("out2", "pid0", "out1", "asg0"):            # back to A = out1, B = asg0
            await pg.locator(pid(type="probe", p=sig)).click()
            await pg.wait_for_timeout(500)
        check("probes restored", (await pg.locator("#pb-out1").inner_text()) == "A"
              and (await pg.locator("#pb-asg0").inner_text()) == "B")

        # ---- Lock Assist: click a crosshair on another peak ----
        print("--- Lock Assist")
        await pg.wait_for_timeout(1500)
        tr = await pg.evaluate(JS_TRACES)
        stars = [x for d in tr if d["sym"] == "star" for x in d["x"]]
        cross = next((d for d in tr if d["name"] == "lock points"), None)
        if cross and stars:
            i = max(range(len(cross["x"])), key=lambda j: min(abs(cross["x"][j] - s) for s in stars))
            ax = "yaxis2"                                            # error row (transmission is row 1)
            px = await pg.evaluate(JS_PIX, [cross["x"][i], 0, ax])
            await pg.mouse.click(*px)
            await pg.wait_for_timeout(1500)
            txt = await pg.locator("#target-text").inner_text()
            labels = [float(str(lab).replace(" mV", "")) for d in tr if d["mode"] == "markers+text"
                      for lab in (d.get("text") or [])]
            want = min(labels, key=lambda v: abs(v - cross["cd"][i] * 1e3)) if labels else None
            got = float(txt.split("at ")[1].split(" mV")[0]) if "Chosen" in txt else None
            check("clicking a crosshair targets the peak at that crossing", got is not None and want is not None
                  and abs(got - want) < 3, f"crossing {cross['cd'][i]*1e3:+.1f} mV -> {txt}; nearest peak {want}")
            await pg.screenshot(path="v4_5_lock_assist.png")
        else:
            check("crosshairs drawn on the error signal", False)
        await pg.click("#btn-tallest")
        await pg.wait_for_timeout(800)
        await pg.click("#btn-phase")
        await pg.wait_for_timeout(7000)
        check("Auto (phase) runs from the phase block", "auto phase done" in await log_text())

        # ---- lock with the switch ----
        print("--- lock")
        await pg.locator(pid(type="hdr", b="measure")).click()
        await pg.wait_for_timeout(300)
        loc = pg.locator("#s-noise_avg")
        await loc.fill("2"); await loc.press("Enter")
        await pg.locator(pid(type="popx", b="measure")).click()
        await flip("lock")
        ok = await wait_status("LOCKED", 40)
        check("Lock switch -> LOCKED; Scan switch off; Lock pill on", ok and "on" not in (await pill("scan")).split()
              and "on" in (await pill("lock")).split())
        await pg.wait_for_timeout(1500)
        fps_lock = await pg.evaluate(JS_FPS)
        print("browser redraws/s while locked:", fps_lock)
        check("redraws >= 10/s while locked", fps_lock >= 9.5, f"{fps_lock}")
        await pg.screenshot(path="v4_6_locked.png")
        rb = await pg.locator("#rb-pid").inner_text()
        check("PID box shows the real i and |i*S|", "|i·S|" in rb, rb.replace("\n", " | "))
        await flip("scan")
        await pg.wait_for_timeout(500)
        check("Scan on while locked asks first; OK -> SCANNING", bool(dialogs) and await wait_status("SCANNING", 10),
              dialogs[-1][:50] if dialogs else "no dialog")
        await pg.wait_for_timeout(1500)
        await flip("lock")
        check("locks again", await wait_status("LOCKED", 40))

        # ---- measurements ----
        print("--- measurements")
        await pg.click("text= noise spectrum"); await pg.wait_for_timeout(300)
        await pg.click("text= loop (Bode)"); await pg.wait_for_timeout(300)
        await pg.click("#btn-noise")
        await pg.wait_for_timeout(9000)
        await wait_status("LOCKED", 30)
        await pg.click("#btn-loop")
        await pg.wait_for_timeout(5000)
        await wait_status("LOCKED", 30)
        lg = await log_text()
        check("Measure noise ran", "noise: residual detuning" in lg)
        check("Measure loop ran", "loop: unity gain" in lg)
        await pg.screenshot(path="v4_7_diag.png", full_page=True)

        # ---- swept-sine Bode plot, point by point ----
        print("--- Bode plot")
        JS_BODE = """() => { const gd = document.querySelector('#bodefig .js-plotly-plot');
          if (!gd || !gd.layout || !gd.data) return {n: -1, title: ''};
          const m = gd.data.filter(d => d.mode === 'lines+markers');
          return {n: m.length ? m[0].x.length : 0, title: (gd.layout.title || {}).text || ''}; }"""
        await pg.locator(pid(type="hdr", b="measure")).click()
        await pg.wait_for_timeout(300)
        for k, v in (("bode_points", "10"), ("bode_start_hz", "4"), ("bode_stop_hz", "300"), ("na_amp_frac", "0.1")):
            loc = pg.locator(f"#s-{k}")
            await loc.fill(v); await loc.press("Enter"); await pg.wait_for_timeout(400)
        await pg.locator(pid(type="popx", b="measure")).click()

        async def bode_wait_done(timeout=150):
            for _ in range(timeout * 2):
                st = await pg.evaluate(JS_BODE)
                if "measuring" not in st["title"] and st["n"] > 0:
                    return st
                await pg.wait_for_timeout(500)
            return await pg.evaluate(JS_BODE)

        await pg.click("#btn-bode")
        seen = []                                      # point counts drawn while the sweep runs
        stat = ""
        for _ in range(240):
            st = await pg.evaluate(JS_BODE)
            if st["n"] >= 0 and (not seen or st["n"] != seen[-1]):
                seen.append(st["n"])
            if "Bode" in await status():
                stat = await status()
            if st["n"] >= 4:
                break
            await pg.wait_for_timeout(250)
        check("closed-loop Bode fills in point by point", len([n for n in seen if 0 < n < 10]) >= 3,
              f"point counts seen: {seen}")
        check("status shows the sweep progress", "Bode" in stat and "MEASURING" in stat, stat[:70])
        await pg.locator("#rb-testgen").scroll_into_view_if_needed()
        await pg.screenshot(path="v4_8_bode_running.png", clip={"x": 0, "y": 0, "width": 1500, "height": 560})
        st = await bode_wait_done()
        check("closed-loop Bode completes with UGF and phase margin", st["n"] == 10 and "unity gain" in st["title"],
              st["title"][-70:])
        await pg.locator("#bodefig").scroll_into_view_if_needed()
        await pg.locator("#bodefig").screenshot(path="v4_9_bode_closed.png")
        await pg.locator("#s-bode_mode").click()
        await pg.wait_for_timeout(250)
        await pg.locator(".dash-dropdown-option", has_text="circuit only").first.click()
        await pg.wait_for_timeout(700)
        await pg.click("#btn-bode")
        await pg.wait_for_timeout(2500)
        st = await bode_wait_done()
        check("circuit-only Bode completes (electronics UGF and margin)", st["n"] == 10 and "Circuit-only" in st["title"]
              and "unity gain" in st["title"], st["title"][-80:])
        check("still locked after the circuit-only sweep", "LOCKED" in await status())
        await pg.locator("#bodefig").screenshot(path="v4_10_bode_circuit.png")
        await pg.locator(pid(type="hdr", b="measure")).click()
        await pg.wait_for_timeout(300)
        loc = pg.locator("#s-bode_points")
        await loc.fill("30"); await loc.press("Enter"); await pg.wait_for_timeout(400)
        await pg.locator(pid(type="popx", b="measure")).click()
        await pg.click("#btn-bode")
        await pg.wait_for_timeout(4000)
        await pg.click("#btn-stop")
        st = await bode_wait_done(60)
        check("Stop ends the sweep early", "stopped" in st["title"], st["title"][-60:])
        await flip("lock")
        check("Lock switch off -> HELD", await wait_status("HELD", 10))
        print("analysis:", (await pg.locator("#analysis").inner_text())[-300:])
        print("log head:\n" + "\n".join(lg.splitlines()[:10]))
        errs = [e for e in errors if "favicon" not in e]
        check("no browser errors", not errs, str(errs[:3]))
        await b.close()
    print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL UI CHECKS PASSED")
    sys.exit(1 if FAILS else 0)

asyncio.run(main())
