// Browser call: speech-to-text and text-to-speech in the browser, conversation and tools on the server.
(() => {
  const $ = (id) => document.getElementById(id);
  const orb = $("orb"), statusEl = $("status"), transcript = $("transcript"), trace = $("trace");
  const phone = $("phone"), input = $("text"), timerEl = $("timer");
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = window.speechSynthesis;

  let session = null, live = false, busy = false, rec = null, listening = false, speaking = false;
  let started = 0, tick = null, voice = null;

  // --- voice selection: prefer a natural US English voice
  function pickVoice() {
    if (!synth) return;
    const vs = synth.getVoices().filter((v) => /^en(-|_)US/i.test(v.lang));
    const prefs = [/Ava/i, /Samantha/i, /Google US English/i, /Jenny/i, /Aria/i, /Natural/i, /Zira/i];
    for (const p of prefs) { const v = vs.find((x) => p.test(x.name)); if (v) { voice = v; return; } }
    voice = vs[0] || null;
  }
  if (synth) { pickVoice(); synth.onvoiceschanged = pickVoice; }

  function setStatus(text, interim) {
    statusEl.innerHTML = "";
    statusEl.append(document.createTextNode(text));
    if (interim) { const s = document.createElement("span"); s.className = "interim"; s.textContent = interim; statusEl.append(s); }
  }

  function orbState() {
    orb.classList.toggle("live", live);
    orb.classList.toggle("listening", live && listening);
    orb.classList.toggle("speaking", live && speaking);
    $("ico-call").style.display = live ? "none" : "";
    $("ico-end").style.display = live ? "" : "none";
    orb.setAttribute("aria-label", live ? "End call" : "Start call");
  }

  function bubble(role, text) {
    const empty = transcript.querySelector(".empty"); if (empty) empty.remove();
    const b = document.createElement("div");
    b.className = "bubble " + (role === "agent" ? "agent" : "caller");
    b.textContent = text;
    transcript.append(b);
    transcript.scrollTop = transcript.scrollHeight;
  }

  function summarizeArgs(args) {
    return Object.entries(args || {}).filter(([, v]) => v !== "" && v !== null && v !== undefined && v !== "any")
      .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(" · ") || "no arguments";
  }

  function addTrace(ev) {
    const empty = trace.querySelector(".empty"); if (empty) empty.remove();
    const row = document.createElement("div");
    row.className = "trace-row";
    const mark = document.createElement("span");
    mark.className = ev.ok ? "ok" : "no"; mark.textContent = ev.ok ? "✓" : "!";
    const body = document.createElement("div");
    const fn = document.createElement("span"); fn.className = "fn"; fn.textContent = ev.tool;
    const args = document.createElement("div"); args.className = "muted"; args.style.fontSize = "12.5px"; args.textContent = summarizeArgs(ev.args);
    const res = document.createElement("div"); res.className = "res";
    res.textContent = ev.result.replace(/\s*\[date=[^\]]+\]/g, "").slice(0, 260);
    body.append(fn, args, res);
    const meta = document.createElement("span"); meta.className = "meta"; meta.textContent = ev.ms + " ms";
    row.append(mark, body, meta);
    trace.append(row);
    trace.scrollTop = trace.scrollHeight;
  }

  function addSms(m) {
    const e = $("phone-empty"); if (e) e.remove();
    const d = document.createElement("div"); d.className = "msg";
    // make the confirmation code clickable: it fills the input, handy for "move my appointment"
    const parts = m.body.split(/(\b[A-Z0-9]{6}\b(?=\.|\)|,|\s|$))/);
    parts.forEach((p) => {
      if (/^[A-Z0-9]{6}$/.test(p) && /\d|[A-Z]{6}/.test(p) && p !== p.toLowerCase()) {
        const c = document.createElement("span"); c.className = "code"; c.textContent = p; c.title = "Click to use this code";
        c.onclick = () => { input.value = `My code is ${p}`; input.focus(); };
        d.append(c);
      } else d.append(document.createTextNode(p));
    });
    phone.append(d);
  }

  function speak(text) {
    return new Promise((resolve) => {
      if (!synth) return resolve();
      synth.cancel();
      const u = new SpeechSynthesisUtterance(text);
      if (voice) u.voice = voice;
      u.rate = 1.03; u.pitch = 1.0;
      speaking = true; orbState(); setStatus("Ava is speaking…");
      const done = () => { speaking = false; orbState(); resolve(); };
      u.onend = done; u.onerror = done;
      synth.speak(u);
      // Chrome sometimes never fires onend for long utterances
      setTimeout(() => { if (speaking && !synth.speaking) done(); }, 400 + text.length * 90);
    });
  }

  async function send(text) {
    if (busy) return;
    busy = true;
    if (text) bubble("caller", text);
    setStatus("Ava is checking…");
    try {
      const r = await fetch("/api/converse", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ session, text: text || "" }),
      });
      if (r.status === 429) { bubble("agent", "The demo line is busy right now. Please try again in a few minutes."); busy = false; return; }
      const data = await r.json();
      session = data.session;
      $("caller-id").textContent = "Calling from " + session.caller.replace(/^\+1(\d{3})(\d{3})(\d{4})$/, "($1) $2-$3");
      data.events.forEach(addTrace);
      data.sms.forEach(addSms);
      $("engine").textContent = data.engine === "llm" ? "LLM + live booking engine" : "Dialogue engine + live booking engine";
      bubble("agent", data.reply);
      busy = false;
      if (!live) setStatus(data.ended ? "Call ended. Press to call again." : "Press the button to talk, or keep typing.");
      if (live) {
        await speak(data.reply);
        if (data.ended) return hangUp("Call ended. Press to call again.");
        listen();
      }
    } catch (e) {
      busy = false;
      bubble("agent", "Sorry, the line dropped. Please try again.");
    }
  }

  function listen() {
    if (!live || !SR || busy) { if (live && !SR) setStatus("Your browser doesn't support voice input. Type below."); return; }
    if (listening) return;
    rec = new SR();
    rec.lang = "en-US"; rec.interimResults = true; rec.continuous = false; rec.maxAlternatives = 1;
    let finalText = "";
    rec.onstart = () => { listening = true; orbState(); setStatus("Listening…"); };
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) finalText += e.results[i][0].transcript; else interim += e.results[i][0].transcript;
      }
      setStatus("Listening…", interim || finalText);
    };
    rec.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        setStatus("Microphone blocked. Allow it in the address bar, or type below.");
        listening = false; orbState(); rec = null;
      }
    };
    rec.onend = () => {
      listening = false; orbState(); rec = null;
      const t = finalText.trim();
      if (!live) return;
      if (t) send(t);
      else if (!busy && !speaking) setTimeout(listen, 250);
    };
    try { rec.start(); } catch (_) { /* already started */ }
  }

  function hangUp(msg) {
    live = false; listening = false;
    if (rec) { try { rec.abort(); } catch (_) {} rec = null; }
    if (synth) synth.cancel();
    clearInterval(tick); orbState();
    setStatus(msg || "Call ended. Press to call again.");
  }

  async function startCall() {
    live = true; orbState();
    started = Date.now();
    tick = setInterval(() => {
      const s = Math.floor((Date.now() - started) / 1000);
      timerEl.textContent = String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
    }, 500);
    if (SR && navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
      try { (await navigator.mediaDevices.getUserMedia({ audio: true })).getTracks().forEach((t) => t.stop()); }
      catch (_) { setStatus("Microphone blocked. You can type below."); }
    }
    if (!session || !session.transcript || session.transcript.length === 0) send("");
    else listen();
  }

  orb.onclick = () => (live ? hangUp() : startCall());

  $("composer").onsubmit = (e) => {
    e.preventDefault();
    const t = input.value.trim(); if (!t) return;
    input.value = "";
    if (rec) { try { rec.abort(); } catch (_) {} }
    if (synth) synth.cancel();
    if (!session) { // typing without starting the call: open the line silently first
      send("").then(() => send(t));
    } else send(t);
  };

  document.querySelectorAll("#chips .chip").forEach((c) => {
    c.onclick = () => { input.value = c.textContent; $("composer").requestSubmit(); };
  });

  if (!SR) setStatus("Press to call. Voice works best in Chrome, Edge or Safari; you can also type.");
})();
