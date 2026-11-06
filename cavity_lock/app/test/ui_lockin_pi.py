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
        await pg.wait_for_timeout(4000)
        check("schematic: 7 switches, blocks, probes", await pg.locator(".sw").count() == 7
              and await pg.locator(".b-pid").count() == 1 and await pg.locator(".b-int_pid").count() == 1
              and await pg.locator(".probe").count() >= 9)
        check("lock RAMP, Output/Ramp/Dither on, PI/Filter off", await wait_text("#status", "LOCK: RAMP")
              and await on("output") and await on("ramp") and await on("dither") and not await on("pi")
              and not await on("filt"))
        check("intensity SERVO OFF, its switches off", "SERVO OFF" in await pg.locator("#status").inner_text()
              and not await on("int_on") and not await on("int_filt"))
        check("wires drawn", await pg.locator("#wires path").count() > 20)
        for name in ("X", "Y", "R", "IN1", "OUT1"):
            check(f"lock scope trace {name}", await pg.locator("#scope .legendtext", has_text=name).count() >= 1)
        xt = await pg.locator("#scope .xtitle").text_content()
        check("ramp scope: 4 ramp periods", "4.0 ramp periods" in xt, xt)
        check("intensity scope IN2 and OUT2", await pg.locator("#iscope .legendtext", has_text="IN2").count() >= 1
              and await pg.locator("#iscope .legendtext", has_text="OUT2").count() >= 1)
        await pg.screenshot(path="lockin_pi_0_ramp.png", full_page=True)
        await typ("phase_deg", 35)
        check("phase typed -> iq2.phase", abs(stats()["phase"] - 35) < 1e-9)
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
        await pg.wait_for_timeout(2000)
        check("PI box shows the integrator", "integrator" in await pg.locator("#rb-pid").inner_text())
        await typ("filt1_hz", 1000)
        check("filter value alone: pid0 filter still off", stats()["filt"] == [0, 0, 0, 0])
        await flip("filt")
        check("Filter switch -> pid0 filter on", await on("filt") and stats()["filt"][0] > 0
              and "LP" in await pg.locator("#rb-filter").inner_text())
        await flip("filt")
        check("Filter switch off", stats()["filt"] == [0, 0, 0, 0])
        await pg.locator("#s-pi_input label", has_text="Y").click(); await pg.wait_for_timeout(900)
        check("PI input Y", stats()["pin"] == "iq2_2")
        await pg.locator("#s-pi_input label", has_text="X").click(); await pg.wait_for_timeout(900)
        await pg.locator(pid(type="probe", p="pid0")).click(); await pg.wait_for_timeout(1500)
        check("probe pid0 -> probe A row", await pg.locator("#scope .legendtext", has_text="A: pid0").count() == 1)
        await typ("int_p", 0.01)
        await typ("int_i", 2000)
        await typ("int_setpoint_mv", 400)
        await flip("int_on")
        st = stats()
        check("Servo switch -> pid2 on OUT2 with the typed gains", await wait_text("#status", "SERVO ON")
              and st["iout"] == "out2" and abs(st["ip"] - 0.01) < 2.5e-4 and abs(st["ii"] - 2000) < 1, str(st))
        await pg.wait_for_timeout(1500)
        txt = await pg.locator("#istats").inner_text()
        check("intensity: IN2 at the setpoint", "IN2 mean +4" in txt or "IN2 mean +39" in txt, txt)
        await typ("int_filt1_hz", 20000)
        await flip("int_filt")
        check("intensity Filter switch", stats()["ifilt"][0] > 0)
        await flip("int_filt")
        await pg.locator(pid(type="hdr", b="pid")).click(); await pg.wait_for_timeout(400)
        check("PI popover with the clamps", await pg.locator("#s-clamp_lo_mv").is_visible())
        await pg.locator(pid(type="popx", b="pid")).click()
        await pg.screenshot(path="lockin_pi_1_locked.png", full_page=True)
        await flip("dither")
        check("Dither switch off", stats()["dither"] == "off" and not await on("dither"))
        await flip("dither")
        await flip("ramp")
        check("Ramp switch -> RAMP, PI off", await wait_text("#status", "LOCK: RAMP") and stats()["out"] == "off")
        before = float(await pg.locator("#s-offset_mv").input_value())
        await pg.locator(".b-ramp .spadj button", has_text="+10").click(); await pg.wait_for_timeout(1500)
        after = float(await pg.locator("#s-offset_mv").input_value())
        check("offset +10 mV button", abs(after - before - 10) < 1e-6, f"{before} -> {after}")
        await flip("output")
        check("lock Output off", await wait_text("#status", "OUTPUT OFF") and stats()["iout"] == "out2")
        await flip("int_on")
        check("intensity Servo off", await wait_text("#status", "SERVO OFF") and stats()["iout"] == "off")
        errs = [e for e in errors if "favicon" not in e]
        check("no browser errors", not errs, str(errs[:3]))
        await b.close()
    print(f"\n{len(FAILS)} FAILED: {FAILS}" if FAILS else "\nALL UI CHECKS PASSED")

asyncio.run(main())
