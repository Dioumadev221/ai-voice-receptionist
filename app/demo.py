"""Public demo page: talk to the receptionist from the browser (Vapi web call)."""

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from jinja2 import Environment, select_autoescape

from app.config import Settings, get_settings

router = APIRouter(tags=["demo"])

PAGE = Environment(autoescape=select_autoescape()).from_string("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ clinic }} - Talk to Ava</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#14181f;--muted:#5d6675;--line:#e3e6eb;--accent:#0f766e;--on:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--ink:#e8ebef;--muted:#98a1ae;--line:#2a3039;--accent:#2dd4bf;--on:#0f1216}}
*{box-sizing:border-box}body{margin:0;font:16px/1.55 system-ui,sans-serif;background:var(--bg);color:var(--ink)}
main{max-width:640px;margin:0 auto;padding:48px 16px}h1{font-size:28px;margin:0 0 8px}p{color:var(--muted)}
button{font:inherit;font-weight:600;border:0;border-radius:999px;padding:14px 28px;background:var(--accent);color:var(--on);cursor:pointer}
button.live{background:#dc2626;color:#fff}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-top:24px}
#log{max-height:320px;overflow:auto;font-size:14px}#log div{padding:4px 0;border-bottom:1px solid var(--line)}#log b{color:var(--accent)}
ul{color:var(--muted);padding-left:20px}
</style></head><body><main>
<h1>Call {{ clinic }}'s AI receptionist</h1>
<p>Ava answers 24/7, books, moves and cancels appointments against the live schedule, and texts a confirmation. Try it from your browser.</p>
<button id="btn" {% if not ready %}disabled{% endif %}>{{ "Start call" if ready else "Demo not configured" }}</button>
<div class="card"><strong>Things to try</strong><ul>
<li>"I'd like a cleaning next Tuesday morning."</li><li>"I chipped a tooth and it really hurts."</li>
<li>"Can I move my appointment? My code is ..."</li><li>"Do you take Delta Dental?"</li></ul></div>
<div class="card"><strong>Live transcript</strong><div id="log"></div></div>
</main>
<script type="module">
import Vapi from "https://esm.sh/@vapi-ai/web@2.7.1";
const btn = document.getElementById("btn"), log = document.getElementById("log");
const vapi = new Vapi({{ public_key|tojson }});
let live = false;
btn.onclick = () => live ? vapi.stop() : (btn.textContent = "Connecting...", vapi.start({{ assistant_id|tojson }}));
vapi.on("call-start", () => { live = true; btn.textContent = "End call"; btn.classList.add("live"); });
vapi.on("call-end", () => { live = false; btn.textContent = "Start call"; btn.classList.remove("live"); });
vapi.on("message", (m) => {
  if (m.type !== "transcript" || m.transcriptType !== "final") return;
  const row = document.createElement("div");
  row.innerHTML = `<b>${m.role === "assistant" ? "Ava" : "You"}:</b> `;
  row.append(document.createTextNode(m.transcript));
  log.append(row); log.scrollTop = log.scrollHeight;
});
vapi.on("error", (e) => { console.error(e); btn.textContent = "Start call"; live = false; });
</script></body></html>""")


@router.get("/demo", response_class=HTMLResponse)
def demo(settings: Settings = Depends(get_settings)):
    return PAGE.render(
        clinic=settings.clinic_name,
        public_key=settings.vapi_public_key,
        assistant_id=settings.vapi_assistant_id,
        ready=bool(settings.vapi_public_key and settings.vapi_assistant_id),
    )
