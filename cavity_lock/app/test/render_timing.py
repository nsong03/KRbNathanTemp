import asyncio, sys
from playwright.async_api import async_playwright
PORT = int(sys.argv[1])
JS = """async () => { const gd = document.querySelector('#scope .js-plotly-plot');
  const pts = gd.data.reduce((a, d) => a + (d.x ? d.x.length : 0), 0);
  const data = gd.data.map(d => Object.assign({}, d, {x: Array.from(d.x || []), y: Array.from(d.y || [])}));
  const lay = JSON.parse(JSON.stringify(gd.layout));
  let t0 = performance.now(); for (let i = 0; i < 5; i++) { await Plotly.react(gd, data.map(d => Object.assign({}, d)), lay); }
  const gl = (performance.now() - t0) / 5;
  const d2 = data.map(d => Object.assign({}, d, {type: d.type === 'scattergl' ? 'scatter' : d.type}));
  t0 = performance.now(); for (let i = 0; i < 5; i++) { await Plotly.react(gd, d2.map(d => Object.assign({}, d)), lay); }
  const svg = (performance.now() - t0) / 5;
  return {points: pts, scattergl_ms: Math.round(gl), svg_ms: Math.round(svg)}; }"""
JS_CB = """() => new Promise(res => { let n = 0, tot = 0; const orig = window.fetch;
  window.fetch = function() { const s = performance.now(); return orig.apply(this, arguments).then(r => { n++; tot += performance.now() - s; return r; }); };
  const gd = document.querySelector('#scope .js-plotly-plot'); let f = 0; gd.on('plotly_afterplot', () => f++);
  setTimeout(() => res({requests_per_s: n / 5, mean_rt_ms: Math.round(tot / Math.max(n, 1)), redraws_per_s: f / 5}), 5000); })"""
async def main():
    async with async_playwright() as p:
        for args in ([], ["--enable-gpu", "--use-angle=swiftshader"]):
            b = await p.chromium.launch(args=args); pg = await b.new_page(viewport={"width": 1500, "height": 1000})
            await pg.goto(f"http://127.0.0.1:{PORT}"); await pg.wait_for_timeout(3000)
            print(args or "default", await pg.evaluate(JS_CB), await pg.evaluate(JS))
            await b.close()
asyncio.run(main())
