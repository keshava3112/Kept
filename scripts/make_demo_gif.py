"""Render docs/assets/demo.gif: one promise's life in Kept, about 12 seconds, looping.

    pip install playwright && playwright install chromium
    python scripts/make_demo_gif.py            # needs ffmpeg on PATH

The scene is plain HTML/CSS driven by a state dict; we screenshot each frame
with headless Chromium and let ffmpeg build an optimised palette GIF.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "assets" / "demo.gif"
W, H, FPS = 960, 540, 15

HTML = r"""
<html><head><style>
*{box-sizing:border-box}
body{margin:0;width:960px;height:540px;background:#f6f5f1;overflow:hidden;
  font-family:Inter,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;color:#1d1c1d}
.panel{position:absolute;background:#fff;border:1px solid #e3e1da;border-radius:12px;
  box-shadow:0 2px 10px rgba(0,0,0,.06);overflow:hidden}
.ph{height:40px;border-bottom:1px solid #eeece6;padding:0 16px;display:flex;align-items:center;
  font-size:13px;font-weight:600;color:#6b6a65;gap:8px}
#slack{left:24px;top:24px;width:528px;height:432px}
#kept{left:572px;top:24px;width:364px;height:236px}
#linear{left:572px;top:276px;width:364px;height:180px}
.msg{display:flex;gap:10px;padding:14px 16px 0}
.av{width:34px;height:34px;border-radius:8px;flex:none;color:#fff;font-weight:700;
  display:flex;align-items:center;justify-content:center;font-size:15px}
.name{font-weight:700;font-size:14px}.time{color:#8a8984;font-size:11px;margin-left:6px;font-weight:400}
.txt{font-size:14px;line-height:1.45;margin-top:2px}
.hl{position:relative}.hl i{position:absolute;left:-2px;top:0;height:100%;background:#fff1b8;
  border-radius:3px;z-index:0}.hl span{position:relative;z-index:1}
.card{margin:14px 16px;border:1px solid #e3e1da;border-radius:10px;padding:12px 14px;position:relative}
.ct{font-size:14px;font-weight:600}.cm{font-size:12px;color:#6b6a65;margin-top:4px}
.q{font-size:12px;color:#55544f;font-style:italic;margin-top:8px;border-left:3px solid #d8d6cf;padding-left:8px}
.pill{margin-left:auto;font-size:11px;font-weight:700;padding:3px 8px;border-radius:99px;white-space:nowrap}
.cm{display:flex;align-items:center}
.tk{margin:14px 16px;display:flex;align-items:center;gap:10px;font-size:14px}
.key{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#6b6a65}
.state{margin-left:auto;font-size:12px;font-weight:600;padding:3px 8px;border-radius:6px}
#dm{position:absolute;left:20px;width:488px;background:#1f2328;color:#fff;border-radius:12px;
  padding:14px 16px;box-shadow:0 10px 30px rgba(0,0,0,.25)}
#dm .h{font-size:12px;color:#a8a7a2;font-weight:600}
#dm .b{font-size:14px;margin-top:6px;line-height:1.45}
.btn{display:inline-block;font-family:ui-monospace,Menlo,monospace;font-size:12px;background:#33383f;
  border-radius:5px;padding:3px 8px;margin:8px 6px 0 0}
#clock{position:absolute;right:24px;top:-2px}
#cap{position:absolute;left:24px;right:24px;top:474px;height:44px;display:flex;align-items:center;gap:12px}
#cap .n{width:26px;height:26px;border-radius:50%;background:#1f2328;color:#fff;font-size:13px;font-weight:700;
  display:flex;align-items:center;justify-content:center;flex:none}
#cap .t{font-size:17px;font-weight:600}
#brand{position:absolute;right:24px;top:480px;font-size:15px;font-weight:800;letter-spacing:-.3px;color:#1f2328}
#brand small{font-weight:500;color:#8a8984;font-size:12px;letter-spacing:0;margin-left:6px}
#clockchip{font-size:12px;font-weight:600;color:#1d1c1d;background:#f1f0eb;border-radius:6px;padding:4px 8px}
</style></head><body>
<div class="panel" id="slack">
  <div class="ph"># kelso <span style="flex:1"></span><span id="clockchip"></span></div>
  <div class="msg" id="m0"><div class="av" style="background:#7d5ba6">P</div><div>
    <div class="name">Priya Rao<span class="time">Mon 10:02 AM</span></div>
    <div class="txt">Kelso wants the revised SOW before their board meets.</div></div></div>
  <div class="msg" id="m1"><div class="av" style="background:#c2410c">S</div><div>
    <div class="name">Sana Iyer<span class="time">Mon 10:04 AM</span></div>
    <div class="txt">On it. <span class="hl"><i id="hlbar"></i><span>I'll send the revised SOW to Meera by Friday.</span></span></div></div></div>
  <div class="msg" id="m2"><div class="av" style="background:#c2410c">S</div><div>
    <div class="name">Sana Iyer<span class="time">Mon 9:12 AM</span></div>
    <div class="txt" style="font-family:ui-monospace,Menlo,monospace;font-size:13px"><span style="color:#2f5bd3;background:#e8eefc;border-radius:3px;padding:0 2px">@kept</span> done 1</div></div></div>
  <div id="dm"><div class="h">kept · direct message</div>
    <div class="b">⏰ Overdue: you said <i>“I'll send the revised SOW to Meera by Friday”</i> in #kelso. Due Fri 6:00 PM.</div>
    <span class="btn">done 1</span><span class="btn">push 1 to mon</span><span class="btn">drop 1</span></div>
</div>
<div class="panel" id="kept">
  <div class="ph"><span style="color:#1f2328;font-weight:800">kept</span> · promises</div>
  <div class="card" id="card">
    <div class="ct">#1 Send the revised SOW to Meera</div>
    <div class="cm" id="meta">Sana · due Fri 6:00 PM<span class="pill" id="pill"></span></div>
    <div class="q">“I'll send the revised SOW to Meera by Friday” · #kelso</div>
  </div>
</div>
<div class="panel" id="linear">
  <div class="ph">Linear · Kelso Rebrand</div>
  <div class="tk" id="tk"><span class="key">KEL-1</span> Revised SOW for Kelso
    <span class="state" id="tstate"></span></div>
  <div class="tk" style="opacity:.45"><span class="key">KEL-2</span> Update moodboard
    <span class="state" style="background:#e6f4ea;color:#1e7a46">Done</span></div>
</div>
<div id="cap"><div class="n" id="capn"></div><div class="t" id="capt"></div></div>
<div id="brand">Kept<small>every promise, kept</small></div>
<script>
const PILL={made:["made","#f1f0eb","#55544f"],linked:["linked · KEL-1","#e8eefc","#2f5bd3"],
  in_progress:["in progress","#fff4dc","#9a6400"],broken:["overdue","#fde8e6","#c0392b"],done:["kept ✓","#e6f4ea","#1e7a46"]};
const TS={todo:["Todo","#f1f0eb","#55544f"],started:["In Progress","#fff4dc","#9a6400"],done:["Done","#2e9e5b","#ffffff"]};
function ease(t){return t<.5?2*t*t:1-Math.pow(-2*t+2,2)/2}
function show(id,p,dy=12){const e=document.getElementById(id);const q=ease(Math.max(0,Math.min(1,p)));
  e.style.opacity=q;e.style.transform=`translateY(${(1-q)*dy}px)`}
window.render=function(s){
  show("m0",s.m0);show("m1",s.m1);show("m2",s.m2);
  document.getElementById("m2").style.display=s.m2>0?"flex":"none";
  document.getElementById("hlbar").style.width=`calc(${ease(s.hl)*100}% + 4px)`;
  const c=document.getElementById("card");const q=ease(Math.min(1,s.card));
  c.style.opacity=q;c.style.transform=`translateX(${(1-q)*-60}px) scale(${.96+.04*q})`;
  c.style.boxShadow=s.glow>0?`0 0 0 ${3*s.glow}px rgba(47,91,211,${.25*s.glow})`:"none";
  const [pt,pb,pc]=PILL[s.status];const pl=document.getElementById("pill");
  pl.textContent=pt;pl.style.background=pb;pl.style.color=pc;
  c.style.borderColor=s.status==="broken"?"#f3b5ad":s.status==="done"?"#9fd8b5":"#e3e1da";
  show("tk",s.tk,8);const [tt,tb,tc]=TS[s.ticket];const st=document.getElementById("tstate");
  st.textContent=tt;st.style.background=tb;st.style.color=tc;
  const dm=document.getElementById("dm");const d=ease(Math.max(0,Math.min(1,s.dm)));
  dm.style.opacity=d;dm.style.top=`${432-(d*190)}px`;
  document.getElementById("clockchip").textContent=s.clock;
  document.getElementById("capn").textContent=s.n;document.getElementById("capt").textContent=s.cap;
};
</script></body></html>
"""

BASE = dict(m0=1, m1=0, m2=0, hl=0, card=0, glow=0, status="made", tk=0, ticket="todo", dm=0,
            clock="Mon 10:03 AM", n="1", cap="Someone makes a promise in a thread")


def timeline() -> list[dict]:
    frames: list[dict] = []
    s = dict(BASE)

    def hold(sec, **kw):
        s.update(kw)
        frames.extend(dict(s) for _ in range(int(sec * FPS)))

    def tween(sec, **targets):
        start = {k: s[k] for k in targets}
        n = max(1, int(sec * FPS))
        for i in range(1, n + 1):
            for k, v in targets.items():
                s[k] = start[k] + (v - start[k]) * i / n
            frames.append(dict(s))

    hold(0.6)
    tween(0.4, m1=1)
    hold(0.4, clock="Mon 10:04 AM")
    hold(0.1, n="2", cap="Kept catches it, with the exact words as a receipt")
    tween(0.7, hl=1)
    tween(0.5, card=1)
    hold(1.0)
    hold(0.1, n="3", cap="…and links it to the ticket that does the work")
    tween(0.4, tk=1)
    tween(0.3, glow=1)
    hold(0.1, status="linked")
    tween(0.4, glow=0)
    hold(0.8)
    hold(0.1, n="4", cap="The ticket moves, so the promise moves with it", clock="Wed 11:20 AM")
    hold(0.3, ticket="started")
    hold(0.2, status="in_progress")
    hold(1.0)
    hold(0.1, n="5", cap="Friday 6 PM passes. Nobody chased it…", clock="Fri 6:00 PM")
    for t in ("Fri 6:00 PM", "Fri 6:01 PM", "Sat 9:00 AM", "Mon 9:00 AM"):
        hold(0.25, clock=t)
    hold(0.2, status="broken")
    hold(0.9)
    hold(0.1, n="6", cap="…so Kept nudges the owner, before the client has to")
    tween(0.5, dm=1)
    hold(1.6)
    hold(0.1, n="7", cap="One reply closes the loop", clock="Mon 9:12 AM")
    tween(0.35, dm=0)
    tween(0.4, m2=1)
    hold(0.3)
    hold(0.1, status="done", ticket="done")
    hold(2.2, cap="Promise kept. Nobody filed anything.")
    return frames


def main() -> None:
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is required")
    frames = timeline()
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H})
        page.set_content(HTML)
        page.wait_for_timeout(200)
        for i, f in enumerate(frames):
            page.evaluate(f"render({json.dumps(f)})")
            page.screenshot(path=f"{tmp}/f{i:04d}.png")
        browser.close()
        palette = f"{tmp}/palette.png"
        src = ["-framerate", str(FPS), "-i", f"{tmp}/f%04d.png"]
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *src,
                        "-vf", "palettegen=max_colors=256:stats_mode=full", palette], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *src, "-i", palette,
                        "-lavfi", "paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
                        "-loop", "0", str(OUT)], check=True)
    print(f"{OUT.relative_to(ROOT)}: {len(frames)} frames, {len(frames) / FPS:.1f}s, "
          f"{OUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
