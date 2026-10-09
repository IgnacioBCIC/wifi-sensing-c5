const WifiApp = (() => {
  const devices = ["rx01", "rx02", "rx03"];
  let pollTimer = null;

  function toast(message, kind="info") {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.className = `toast alert alert-${kind}`;
    el.style.display = "block";
    setTimeout(() => el.style.display = "none", 4500);
  }

  async function api(url, options={}) {
    const response = await fetch(url, {
      headers: {"Content-Type": "application/json", ...(options.headers || {})},
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function badge(el, text, kind) {
    if (!el) return;
    el.textContent = text;
    el.className = `badge ${kind ? `badge-${kind}` : ""}`;
  }

  function batteryText(bat) {
    if (!bat || bat.valid === false) return "—";
    const mv = Number(bat.voltage_mv);
    const pct = Number(bat.percent_estimate);
    const v = Number.isFinite(mv) ? `${(mv/1000).toFixed(3)} V` : "—";
    const p = Number.isFinite(pct) ? `${pct}%` : "—";
    return `${p} · ${v}`;
  }

  function uptime(ms) {
    if (ms === null || ms === undefined) return "—";
    let s = Math.floor(Number(ms)/1000);
    const d = Math.floor(s/86400); s %= 86400;
    const h = Math.floor(s/3600); s %= 3600;
    const m = Math.floor(s/60);
    return `${d ? d+"d " : ""}${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}`;
  }

  function renderDevices(system) {
    const mqtt = document.getElementById("mqttBadge");
    badge(mqtt, system.mqtt_connected ? "MQTT conectado" : "MQTT desconectado", system.mqtt_connected ? "success" : "error");

    devices.forEach(name => {
      const d = system.devices?.[name] || {};
      badge(
        document.getElementById(`online-${name}`),
        d.online ? "ONLINE" : "OFFLINE",
        d.online ? "success" : "error"
      );
      document.getElementById(`battery-${name}`).textContent = batteryText(d.battery);
      document.getElementById(`rssi-${name}`).textContent = d.rssi !== null && d.rssi !== undefined ? `${d.rssi} dBm` : "—";
      document.getElementById(`ip-${name}`).textContent = d.ip || "—";
      document.getElementById(`channel-${name}`).textContent = d.channel ? `${d.band || ""} · ch ${d.channel}` : "—";
      document.getElementById(`fw-${name}`).textContent = d.firmware_version ? `${d.firmware_version} / ${d.ota_slot || "—"}` : "—";

      const h = d.health;
      let healthText = "Sin prueba";
      if (h) {
        const ratio = Number(h.unique_ratio);
        healthText = `${h.status || "?"} · ${Number.isFinite(ratio) ? (ratio*100).toFixed(0)+"%" : "—"}`;
      }
      document.getElementById(`health-${name}`).textContent = healthText;
      document.getElementById(`meta-${name}`).textContent =
        `MAC: ${d.mac || "—"} · IDF: ${d.idf_version || "—"} · uptime: ${uptime(d.uptime_ms)}`;
    });
  }

  function renderAeEvent(evt, live = false) {
    const branches = evt.branches || {};
    const spectral = branches.spectral || {};
    const delta = branches.delta || {};
    const sr = Number(spectral.ratio) || 0;
    const dr = Number(delta.ratio) || 0;
    const combined = Number(evt.score_ratio ?? evt.score_mean) || Math.max(sr, dr);
    const row = (label, branch) => {
      const score = Number(branch.score_mean) || 0;
      const threshold = Number(branch.threshold) || 0;
      const ratio = Number(branch.ratio) || 0;
      const width = Math.max(0, Math.min(100, ratio * 50));
      return `<div class="prob-row"><strong>${label}</strong><div class="prob-track"><div class="prob-bar" style="width:${width}%"></div></div><span>${score.toFixed(3)} / ${threshold.toFixed(3)} · ${ratio.toFixed(2)}×</span></div>`;
    };
    return `<div class="card" style="margin-top:10px"><div class="card-body">
      <div class="card-title"><span>Bloque ${evt.block_id ?? "?"} · AE híbrido</span><span class="badge ${evt.present ? "badge-warning" : "badge-success"}">${evt.present ? "PRESENCIA" : "AUSENCIA"}</span></div>
      <div style="font-size:2rem;font-weight:800;margin:6px 0 14px 0">${combined.toFixed(2)}× umbral</div>
      <div class="model-probs">${row("Spectral", spectral)}${row("Delta", delta)}</div>
      <div class="spacer"></div>
      <div class="muted">Decisión híbrida: Spectral OR Delta. Un ratio &gt; 1 activa esa rama.</div>
    </div></div>`;
  }

  function renderModelEvents(model) {
    const target = document.getElementById("modelResults");
    if (!target) return;
    if (!Array.isArray(model) || !model.length) {
      target.innerHTML = "";
      return;
    }
    const cards = model.map(evt => {
      if (evt._model_family === "ae_hybrid_v1" || evt.branches) {
        return renderAeEvent(evt, false);
      }
      const probs = evt.probabilities || {};
      const rows = Object.entries(probs).map(([name, value]) => {
        const pct = Number(value) * 100;
        return `<div class="prob-row"><strong>${name}</strong><div class="prob-track"><div class="prob-bar" style="width:${Math.max(0,Math.min(100,pct))}%"></div></div><span>${pct.toFixed(1)}%</span></div>`;
      }).join("");
      return `<div class="card" style="margin-top:10px"><div class="card-body">
        <div class="card-title"><span>Bloque ${evt.block_id ?? "?"}</span><span class="badge ${evt.present ? "badge-warning" : "badge-success"}">${evt.present ? "PRESENCIA" : "AUSENCIA"}</span></div>
        <div class="model-probs">${rows}</div>
        <div class="spacer"></div>
        <div class="muted">P(presencia): ${((Number(evt.presence_probability)||0)*100).toFixed(1)}% · clase: ${evt.predicted_class || "—"}</div>
      </div></div>`;
    });
    target.innerHTML = cards.join("");
  }

  function renderLiveModelEvents(model) {
    const target = document.getElementById("modelResults");
    if (!target) return;
    if (!Array.isArray(model) || !model.length) {
      target.innerHTML = `<div class="muted">Esperando el primer bloque del modelo…</div>`;
      return;
    }

    const evt = model[model.length - 1];
    if (evt._model_family === "ae_hybrid_v1" || evt.branches) {
      const recent = model.slice(-10).reverse().map(item => {
        const ratio = Number(item.score_ratio ?? item.score_mean) || 0;
        return `<span class="badge ${item.present ? "badge-warning" : "badge-success"}">#${item.block_id ?? "?"} · ${ratio.toFixed(2)}×</span>`;
      }).join(" ");
      target.innerHTML = renderAeEvent(evt, true) + `<div class="spacer"></div><div class="kpi-row">${recent}</div>`;
      return;
    }
    const probs = evt.probabilities || {};
    const presence = Math.max(0, Math.min(1, Number(evt.presence_probability) || 0));
    const rows = ["empty", "lying", "sitting", "walking"].map(name => {
      const pct = (Number(probs[name]) || 0) * 100;
      return `<div class="prob-row"><strong>${name}</strong><div class="prob-track"><div class="prob-bar" style="width:${Math.max(0,Math.min(100,pct))}%"></div></div><span>${pct.toFixed(1)}%</span></div>`;
    }).join("");

    const recent = model.slice(-10).reverse().map(item => {
      const p = (Number(item.presence_probability) || 0) * 100;
      return `<span class="badge ${item.present ? "badge-warning" : "badge-success"}">#${item.block_id ?? "?"} · ${p.toFixed(0)}%</span>`;
    }).join(" ");

    target.innerHTML = `
      <div class="card" style="margin-top:10px">
        <div class="card-body">
          <div class="card-title">
            <span>Bloque actual #${evt.block_id ?? "?"}</span>
            <span class="badge ${evt.present ? "badge-warning" : "badge-success"}">${evt.present ? "PRESENCIA" : "AUSENCIA"}</span>
          </div>
          <div style="font-size:2rem;font-weight:800;margin:6px 0 14px 0">${(presence*100).toFixed(1)}% presencia</div>
          <div class="model-probs">${rows}</div>
          <div class="spacer"></div>
          <div class="muted">Clase dominante: ${evt.predicted_class || "—"} · cada bloque se evalúa independientemente.</div>
          <div class="spacer"></div>
          <div class="kpi-row">${recent}</div>
        </div>
      </div>`;
  }

  function renderJob(job) {
    const empty = document.getElementById("jobEmpty");
    const panel = document.getElementById("jobPanel");
    const cancel = document.getElementById("cancelButton");
    if (!empty || !panel) return;

    if (!job) {
      empty.style.display = "block";
      panel.style.display = "none";
      cancel.style.display = "none";
      return;
    }

    empty.style.display = "none";
    panel.style.display = "block";

    badge(document.getElementById("jobState"), job.state, ["completed"].includes(job.state) ? "success" : ["failed","cancelled"].includes(job.state) ? "error" : "info");
    badge(document.getElementById("jobKind"), job.kind);
    badge(document.getElementById("jobBand"), job.band);
    const baselineBadge = document.getElementById("jobBaseline");
    if (baselineBadge) {
      if (job.kind === "health") {
        baselineBadge.textContent = "health";
      } else if (job.kind === "calibration") {
        baselineBadge.textContent = "creando baseline";
      } else if (job.kind === "ae_calibration") {
        baselineBadge.textContent = "calibrando AE híbrido";
      } else if (job.model_mode === "zero_shot_a") {
        baselineBadge.textContent = job.baseline_name || "Original ESPectre";
      } else if (job.model_mode === "ae_hybrid_v1") {
        baselineBadge.textContent = job.model_id || "AE híbrido";
      } else {
        baselineBadge.textContent = "sin modelo";
      }
    }
    if (cancel) {
      cancel.textContent = ["live","ae_calibration"].includes(job.kind) ? "Detener captura" : "Cancelar";
    }

    document.getElementById("jobMessage").textContent = job.message || "";

    const progress = job.progress || {};
    let pct = 0;
    if (job.kind === "live" && ["running","stopping"].includes(job.state)) pct = 100;
    else if (progress.blocks > 0) pct = Math.min(100, (progress.block/progress.blocks)*100);
    if (job.state === "completed") pct = 100;
    document.getElementById("jobProgress").style.width = `${pct}%`;

    cancel.style.display = ["queued","countdown","starting","running","stopping"].includes(job.state) ? "inline-flex" : "none";
    if (["live","ae_calibration"].includes(job.kind)) renderLiveModelEvents(job.summary?.model || []);
    else renderModelEvents(job.summary?.model || []);
  }

  async function poll() {
    try {
      const data = await api("/api/status");
      renderDevices(data.system);
      renderJob(data.job);
      badge(
        document.getElementById("modelCompatBadge"),
        data.config.espectre_zero_shot_a_compatible ? "CSI 200 Hz / 400 · modelos compatibles" : "Configuración no compatible",
        data.config.espectre_zero_shot_a_compatible ? "success" : "warning"
      );
    } catch (err) {
      badge(document.getElementById("mqttBadge"), "Web/API sin respuesta", "error");
    }
  }

  function bindDashboard() {
    const health = document.getElementById("healthButton");
    const experiment = document.getElementById("experimentButton");
    const live = document.getElementById("liveButton");
    const cancel = document.getElementById("cancelButton");
    const experimentBand = document.getElementById("experimentBand");
    const modelMode = document.getElementById("modelMode");
    const baselineSelect = document.getElementById("baselineSelect");

    async function refreshBaselineOptions() {
      if (!baselineSelect || !experimentBand) return;

      const previous = baselineSelect.value || "published";
      const data = await api("/api/calibrations");
      const band = experimentBand.value;

      const options = [
        {id: "published", label: "Original ESPectre (publicado)"},
        ...(data.profiles || [])
          .filter(profile => profile.status === "ready" && profile.band === band)
          .map(profile => ({
            id: profile.id,
            label: `${profile.name} · local`,
          })),
      ];

      baselineSelect.innerHTML = options
        .map(option => `<option value="${option.id}">${option.label}</option>`)
        .join("");

      baselineSelect.value = options.some(option => option.id === previous)
        ? previous
        : "published";

      baselineSelect.disabled = modelMode?.value !== "zero_shot_a";
    }

    experimentBand?.addEventListener("change", () => {
      refreshBaselineOptions().catch(err => toast(err.message, "error"));
    });

    modelMode?.addEventListener("change", () => {
      if (baselineSelect) {
        baselineSelect.disabled = modelMode.value !== "zero_shot_a";
      }
    });

    refreshBaselineOptions().catch(err => toast(err.message, "error"));

    health?.addEventListener("click", async () => {
      health.disabled = true;
      try {
        await api("/api/jobs/health", {
          method: "POST",
          body: JSON.stringify({
            band: document.getElementById("healthBand").value,
            devices,
          }),
        });
        toast("Healthcheck iniciado", "success");
        await poll();
      } catch (err) {
        toast(err.message, "error");
      } finally {
        health.disabled = false;
      }
    });

    experiment?.addEventListener("click", async () => {
      experiment.disabled = true;
      try {
        await api("/api/jobs/experiment", {
          method: "POST",
          body: JSON.stringify({
            band: document.getElementById("experimentBand").value,
            baseline_id: document.getElementById("baselineSelect").value,
            model_mode: document.getElementById("modelMode").value,
            countdown_s: Number(document.getElementById("countdown").value),
            devices,
          }),
        });
        toast("Captura programada.", "success");
        await poll();
      } catch (err) {
        toast(err.message, "error");
      } finally {
        experiment.disabled = false;
      }
    });

    live?.addEventListener("click", async () => {
      live.disabled = true;
      try {
        await api("/api/jobs/live", {
          method: "POST",
          body: JSON.stringify({
            band: document.getElementById("experimentBand").value,
            baseline_id: document.getElementById("baselineSelect").value,
            model_mode: document.getElementById("modelMode").value,
            countdown_s: Number(document.getElementById("countdown").value),
            devices,
          }),
        });
        toast("Captura en vivo programada. Cada bloque actualizará la inferencia.", "success");
        await poll();
      } catch (err) {
        toast(err.message, "error");
      } finally {
        live.disabled = false;
      }
    });

    cancel?.addEventListener("click", async () => {
      try {
        await api("/api/jobs/cancel", {method:"POST", body:"{}"});
        toast("Trabajo cancelado", "warning");
        await poll();
      } catch (err) {
        toast(err.message, "error");
      }
    });

    poll();
    pollTimer = setInterval(poll, 2000);
  }

  function bindConfigPage() {
    const form = document.getElementById("configForm");
    if (!form) return;

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const values = {};
      document.querySelectorAll(".config-field").forEach(el => {
        const key = el.dataset.key;
        if (el.type === "checkbox") values[key] = el.checked;
        else if (el.dataset.type === "float") values[key] = Number.parseFloat(el.value);
        else values[key] = Number.parseInt(el.value, 10);
      });

      try {
        const data = await api("/api/config", {
          method:"PUT",
          body:JSON.stringify({values}),
        });
        document.getElementById("rawConfig").textContent = JSON.stringify(data.raw, null, 2);
        document.getElementById("nominalMs").textContent = `${data.derived.nominal_duration_ms} ms`;
        badge(
          document.getElementById("configCompat"),
          data.derived.espectre_zero_shot_a_compatible ? "Zero-shot A compatible" : "Zero-shot A no compatible",
          data.derived.espectre_zero_shot_a_compatible ? "success" : "warning"
        );
        toast("experiment.json actualizado y respaldado", "success");
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (document.getElementById("devicesGrid")) bindDashboard();
  });

  return {bindConfigPage, poll};
})();
