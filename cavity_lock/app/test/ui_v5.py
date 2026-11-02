"""Browser test of the two views (Playwright). Start `python serve_v3.py` first (fake hardware, port 8070).
View icons and status dots; switching views while both loops are locked writes nothing to the hardware; every
intensity block parameter through its own input; every intensity switch, the probes, the setpoint adjuster, the
Bode measurement; redraws per second in each view; screenshots."""
import asyncio
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, "..")
import cavity_lock_app as A                 # noqa: E402  (FIELDS; no hardware is touched)
import intensity_schematic as ISCH          # noqa: E402
from playwright.async_api import async_playwright   # noqa: E402

PORT = 8070
LAUNCH = dict(executable_path=os.environ.get("CHROME", "/opt/pw-browsers/chromium"), args=["--no-sandbox"]) \
    if os.path.exists(os.environ.get("CHROME", "/opt/pw-browsers/chromium")) else {}
FIELD = {f[0]: f for f in A.FIELDS}
NEW = dict(int_filter_hz="200000", int_filter_stages="2", int_setpoint_mv="480", int_gain_mode="manual",
           int_ugf_khz="20", int_p="0.002", int_i_hz="1000", int_corner_khz="500", int_clamp_auto=None,
           int_clamp_margin="0.1", int_clamp_lo_mv="-30", int_clamp_hi_mv="120", int_out2_limit_mv="250",
           int_scan_lo_mv="-150", int_scan_hi_mv="150", int_scan_hz="80", int_scan_avg="2",
           int_mw_per_v="2", int_pd_bw_mhz="10", int_aom_max_dbm="30", int_aom_delay_us="0.3",
           int_amp_gain_db="31", int_if_full_ma="20", int_rf_src_dbm="3", int_bias_v="1.5")
FAILS = []


def check(name, ok, info=""):
    print(f"   [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({info})" if info else ""), flush=True)
    if not ok:
        FAILS.append(name)


def pid(**kw):
    return "[id='" + json.dumps(kw, sort_keys=True, separators=(",", ":")) + "']"


def server(path):
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5).read())


def js_fps(graph):
    return f"""() => new Promise(res => {{ const gd = document.querySelector('#{graph} .js-plotly-plot'); let n = 0;
      gd.on('plotly_afterplot', () => n++); setTimeout(() => res(n / 5), 5000); }})"""


JS_ITRACES = """() => { const gd = document.querySelector('#iscope .js-plotly-plot');
  return gd && gd.data ? gd.data.map(d => d.name) : []; }"""
JS_IBODE = """() => { const gd = document.querySelector('#ibodefig .js-plotly-plot');
  if (!gd || !gd.layout || !gd.data) return {n: -1, title: ''};
  const m = gd.data.filter(d => d.mode === 'lines+markers');
  return {n: m.length ? m[0].x.length : 0, title: (gd.layout.title || {}).text || ''}; }"""


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(**LAUNCH)
        ctx = await b.new_context(viewport={"width": 1500, "height": 1000})
        pg = await ctx.new_page()
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

        async def ipill(name):
            return (await pg.locator(f"#i-swp-{name}").get_attribute("class")).split()

        async def iflip(name):
            await pg.locator(pid(type="isw", s=name)).click()
            await pg.wait_for_timeout(900)

        async def dot(which):
            return await pg.locator(f"#dot-{which}").get_attribute("class")

        async def view(which):
            await pg.click("#vbtn-int" if which == "intensity" else "#vbtn-cav")
            await pg.wait_for_timeout(700)

        await pg.goto(f"http://127.0.0.1:{PORT}")
        await pg.wait_for_timeout(4000)
        print("--- icons and views")
        check("two view icons with status dots at the top left", await pg.locator(".vicon").count() == 2
              and await pg.locator("#dot-cav").count() == 1 and await pg.locator("#dot-int").count() == 1)
        check("fresh browser: Cavity lock view shown", await pg.locator("#view-cavity").is_visible()
              and not await pg.locator("#view-intensity").is_visible())
        check("dots: cavity scanning (blue), intensity off (grey)", "busy" in await dot("cav")
              and "off" in await dot("int"), f"{await dot('cav')} / {await dot('int')}")
        fps = await pg.evaluate(js_fps("scope"))
        check("cavity view redraws >= 7/s while scanning", fps >= 7, f"{fps}")

        # ---- lock both loops through the UI ----
        print("--- lock both loops")
        if "LOCKED" not in await status():           # a reused server may already be locked
            await pg.locator(pid(type="sw", s="lock")).click()
        check("cavity LOCKED (Lock switch)", await wait_status("LOCKED", 60))
        await view("intensity")
        check("Intensity view shown after a click, cavity hidden", await pg.locator("#view-intensity").is_visible()
              and not await pg.locator("#view-cavity").is_visible())
        check("intensity status: OUTPUT OFF", await wait_status("Intensity: OUTPUT OFF", 5))
        await iflip("output")
        check("Output on -> HELD, Output pill on", await wait_status("HELD", 5) and "on" in await ipill("output"))
        await iflip("scan")
        check("Scan on -> SCANNING, Scan pill on", await wait_status("SCANNING", 5) and "on" in await ipill("scan"))
        await pg.wait_for_timeout(2500)
        names = await pg.evaluate(JS_ITRACES)
        check("the power scan is drawn (light vs OUT2)", "photodiode (in2)" in names, str(names))
        await pg.screenshot(path="v5_2_scan.png")
        await iflip("lock")
        check("Lock (from scanning) -> LOCKED", await wait_status("Intensity: LOCKED", 10)
              and "on" in await ipill("lock") and "on" not in await ipill("scan"))
        await pg.wait_for_timeout(1500)
        check("dots: both green", "ok" in await dot("cav") and "ok" in await dot("int"),
              f"{await dot('cav')} / {await dot('int')}")
        rb = await pg.locator("#i-rb-i_pid").inner_text()
        check("PI box shows p, i, K and |i·K|", "|i·K|" in rb and "K -" in rb, rb.replace("\n", " | "))
        fps_i = await pg.evaluate(js_fps("iscope"))
        check("intensity view redraws >= 10/s locked", fps_i >= 9.5, f"{fps_i}")
        await pg.screenshot(path="v5_1_intensity_locked.png", full_page=True)

        # ---- view switching writes nothing ----
        print("--- view switching while both loops are locked")
        w0 = server("/_test/writes")
        for v in ("cavity", "intensity") * 5:
            await view(v)
        w1 = server("/_test/writes")
        st = server("/_test/stats")
        check("10 view switches: no register write except the shared scope", w1["nonscope"] == w0["nonscope"],
              f"{w1['nonscope'] - w0['nonscope']} writes; " + str(w1["last"][-2:]))
        check("both still locked", st["cav_mode"] == "locked" and st["int_mode"] == "locked", str(st))
        check("the server follows the visible view (schedule hint)", st["view"] == "intensity")
        await view("cavity")
        fps_c = await pg.evaluate(js_fps("scope"))
        check("cavity view redraws >= 10/s locked (intensity locked, hidden)", fps_c >= 9.5, f"{fps_c}")
        await pg.screenshot(path="v5_0_cavity_locked.png")
        await pg.reload()
        await pg.wait_for_timeout(3000)
        check("the last view is remembered after a reload", await pg.locator("#view-cavity").is_visible())
        await view("intensity")
        await pg.reload()
        await pg.wait_for_timeout(3000)
        check("intensity view remembered after a reload", await pg.locator("#view-intensity").is_visible())

        # ---- popover ----
        await pg.locator(pid(type="hdr", b="i_limiter")).click()
        await pg.wait_for_timeout(400)
        check("clicking a block title opens its popover", await pg.locator(pid(type="pop", b="i_limiter")).is_visible())
        await pg.screenshot(path="v5_3_popover.png", clip={"x": 0, "y": 60, "width": 1500, "height": 600})
        await pg.locator(pid(type="popx", b="i_limiter")).click()
        await pg.wait_for_timeout(300)

        # ---- every intensity block parameter through its own input ----
        print("--- editing every intensity block parameter")

        async def set_value(key, val):
            kind = FIELD[key][4]
            loc = pg.locator(f"#s-{key}")
            if kind == "check":
                await loc.locator("input").click()
            elif kind == "select":
                opt = pg.locator(".dash-dropdown-option").filter(has_text=re.compile("^" + re.escape(str(val)) + "$"))
                for _ in range(3):
                    await loc.click()
                    await pg.wait_for_timeout(300)
                    if await opt.count() and await opt.first.is_visible():
                        break
                await opt.first.scroll_into_view_if_needed()
                await opt.first.click()
            else:
                await loc.fill(str(val))
                await loc.press("Enter")
            await pg.wait_for_timeout(700)

        async def get_value(key):
            kind = FIELD[key][4]
            loc = pg.locator(f"#s-{key}")
            if kind == "check":
                return await loc.locator("input").is_checked()
            if kind == "select":
                return (await loc.inner_text()).strip()
            return await loc.input_value()

        for blk in ISCH.BLOCKS:
            for prm in blk["params"]:
                k = prm["key"]
                if not prm["inline"]:
                    await pg.locator(pid(type="hdr", b=blk["id"])).click()
                    await pg.wait_for_timeout(300)
                old = await get_value(k)
                new = NEW[k]
                if k == "int_gain_mode":
                    new = "manual" if old != "manual" else "bandwidth"
                await set_value(k, new)
                lg = await log_text()
                head = "\n".join(lg.splitlines()[:8])
                ok = f"{k}=" in head or (k == "int_setpoint_mv" and f"setpoint {new} mV" in head)
                rb = await pg.locator(f"#i-rb-{blk['id']}").inner_text()
                check(f"{blk['id']}: {k} {old} -> {new if new is not None else 'toggled'}", ok,
                      rb.replace("\n", " | ")[:80])
                await set_value(k, old)
                if not prm["inline"]:
                    await pg.locator(pid(type="popx", b=blk["id"])).click()
                    await pg.wait_for_timeout(200)
        await pg.wait_for_timeout(1000)
        check("still locked after the parameter edits", await wait_status("Intensity: LOCKED", 15))
        tip = await pg.locator("#i-d-dropdown").get_attribute("title")
        check("D dropdown is shown disabled with the reason", "not in this FPGA build" in (tip or ""), tip)

        # ---- setpoint adjuster ----
        print("--- setpoint adjuster")
        n_btn = await pg.locator(".spadj button").count()
        print("setpoint buttons:", n_btn, await pg.evaluate("() => Array.from(document.querySelectorAll("
                                                            "'.spadj button')).map(e => e.id)"))
        await pg.locator(".spadj button", has_text=re.compile(r"^\+10$")).click()
        await pg.wait_for_timeout(1200)
        check("+10 mV button -> setpoint 510 mV (status and input follow)", "setpoint 510 mV" in await status()
              and (await pg.locator("#s-int_setpoint_mv").input_value()) == "510", await status())
        await pg.locator(".spadj button", has_text=re.compile("^\u22120\\.1$")).click()
        await pg.wait_for_timeout(1000)
        check("−0.1 mV button", "setpoint 509.9 mV" in await status(), await status())
        sl = pg.locator(".spslider .dash-slider-root").first
        bb = await sl.bounding_box()
        await pg.mouse.click(bb["x"] + bb["width"] * 0.45, bb["y"] + bb["height"] / 2)
        await pg.wait_for_timeout(1500)
        stt = await status()
        mv = float(re.search(r"setpoint ([\d.]+) mV", stt).group(1))
        check("the slider sets the setpoint", abs(mv - 509.9) > 1 and 50 < mv < 900, stt)
        await set_value("int_setpoint_mv", "500")
        await pg.wait_for_timeout(800)
        check("typed setpoint 500 mV; still locked", "setpoint 500 mV" in await status()
              and await wait_status("Intensity: LOCKED", 10), await status())

        # ---- probes ----
        print("--- probes")
        await pg.locator(pid(type="iprobe", p="asg1")).click()
        await pg.wait_for_timeout(500)
        await pg.locator(pid(type="iprobe", p="out2")).click()
        await pg.wait_for_timeout(1500)
        names = await pg.evaluate(JS_ITRACES)
        check("probes asg1 / out2 reach the scope", any("asg1" in n for n in names) and any("out2" in n for n in names),
              str(names))
        check("probe badges on the diagram", sorted([await pg.locator("#i-pb-asg1").inner_text(),
                                                     await pg.locator("#i-pb-out2").inner_text()]) == ["A", "B"])
        tip = await pg.locator("#i-pb-err").evaluate("e => e.parentElement.title")
        check("grey probe point (error inside pid2) has a tooltip", "not probe-able" in tip, tip[:60])
        for sig in ("asg1", "out2", "in2", "pid2"):
            await pg.locator(pid(type="iprobe", p=sig)).click()
            await pg.wait_for_timeout(400)
        check("probes restored to in2 / pid2", (await pg.locator("#i-pb-in2").inner_text()) == "A"
              and (await pg.locator("#i-pb-pid2").inner_text()) == "B")

        # ---- switches ----
        print("--- switches")
        await iflip("reengage")
        off = "on" not in await ipill("reengage")
        await iflip("reengage")
        check("Re-engage switch toggles", off and "on" in await ipill("reengage"))
        await iflip("scan")
        await pg.wait_for_timeout(500)
        check("Scan on while locked asks first; OK -> SCANNING", any("intensity servo is locked" in d for d in dialogs)
              and await wait_status("SCANNING", 10), dialogs[-1][:50] if dialogs else "no dialog")
        await pg.wait_for_timeout(1500)
        await iflip("scan")
        check("Scan off -> HELD", await wait_status("HELD", 5))
        await iflip("output")
        check("Output off -> OUTPUT OFF, OUT2 wire dead", await wait_status("OUTPUT OFF", 5)
              and "off" in (await pg.locator("#i-wire-lim_sum").get_attribute("class")))
        await iflip("lock")
        check("Lock from off -> LOCKED (with the last scan)", await wait_status("Intensity: LOCKED", 10))
        await iflip("lock")
        check("Lock off -> HELD", await wait_status("HELD", 5))
        await iflip("lock")
        check("Lock on again", await wait_status("Intensity: LOCKED", 10))

        # ---- Bode plot ----
        print("--- Bode plot")
        await pg.locator("#s-int_panels").get_by_text("Bode (swept)").click()
        await pg.wait_for_timeout(300)
        await pg.locator(pid(type="hdr", b="i_measure")).click()
        await pg.wait_for_timeout(300)
        for k, v in (("int_bode_points", "16"), ("int_bode_start_hz", "2000"), ("int_bode_stop_hz", "300000"),
                     ("int_bode_cycles", "200")):
            loc = pg.locator(f"#s-{k}")
            await loc.fill(v); await loc.press("Enter"); await pg.wait_for_timeout(400)
        await pg.locator(pid(type="popx", b="i_measure")).click()

        async def bode_wait_done(timeout=120):
            for _ in range(timeout * 2):
                st = await pg.evaluate(JS_IBODE)
                if "measuring" not in st["title"] and st["n"] > 0:
                    return st
                await pg.wait_for_timeout(500)
            return await pg.evaluate(JS_IBODE)

        await pg.click("#btn-ibode")
        seen, stat = [], ""
        for _ in range(240):
            st = await pg.evaluate(JS_IBODE)
            if st["n"] >= 0 and (not seen or st["n"] != seen[-1]):
                seen.append(st["n"])
            if "Bode" in await status():
                stat = await status()
            if st["n"] >= 16 or "measuring" not in st["title"] and st["n"] > 0:
                break
            await pg.wait_for_timeout(150)
        check("intensity Bode fills in point by point", len([n for n in seen if 0 < n < 16]) >= 2, f"seen {seen}")
        check("status shows the sweep progress", "MEASURING" in stat and "Bode" in stat, stat[:70])
        st = await bode_wait_done()
        check("intensity Bode completes with UGF and phase margin", st["n"] == 16 and "unity gain" in st["title"],
              st["title"][-70:])
        await pg.locator("#ibodefig").scroll_into_view_if_needed()
        await pg.locator("#ibodefig").screenshot(path="v5_4_bode.png")
        check("still locked after the Bode sweep", await wait_status("Intensity: LOCKED", 10))
        await pg.locator(pid(type="hdr", b="i_measure")).click()
        await pg.wait_for_timeout(300)
        loc = pg.locator("#s-int_bode_points")
        await loc.fill("60"); await loc.press("Enter"); await pg.wait_for_timeout(400)
        await pg.locator(pid(type="popx", b="i_measure")).click()
        await pg.click("#btn-ibode")
        await pg.wait_for_timeout(1500)
        await pg.click("#btn-istop")
        st = await bode_wait_done(60)
        check("Stop ends the intensity sweep early", "stopped" in st["title"], st["title"][-60:])
        await view("cavity")
        check("cavity still LOCKED after all intensity work", await wait_status("Cavity: LOCKED", 10))
        await view("intensity")
        await pg.wait_for_timeout(2000)
        await pg.screenshot(path="v5_5_intensity_full.png", full_page=True)
        errs = [e for e in errors if "favicon" not in e]
        check("no browser errors", not errs, str(errs[:3]))
        await b.close()
    print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL UI CHECKS PASSED")
    sys.exit(1 if FAILS else 0)

asyncio.run(main())
