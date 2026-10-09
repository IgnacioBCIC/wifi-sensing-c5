(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));

  async function api(url, options = {}) {
    const response = await fetch(url, {
      headers: {"Content-Type": "application/json", ...(options.headers || {})},
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function toast(message) {
    const el = $("toast");
    if (!el) return;
    el.textContent = message;
    el.style.display = "block";
    setTimeout(() => { el.style.display = "none"; }, 3200);
  }

  function bandLabel(band) { return band === "5g" ? "5 GHz" : "2.4 GHz"; }

  function profileCard(p) {
    const tech = p.technical || {};
    const statusClass = p.status === "ready" ? "badge-success" : (p.status === "failed" ? "badge-error" : "badge-warning");
    const canUse = p.status === "ready";
    return `
      <article class="card">
        <div class="card-body">
          <div class="card-title"><span>${esc(p.name)}</span><span class="badge ${statusClass}">${esc(p.status)}</span></div>
          <div class="stat-grid">
            <div class="stat"><div class="stat-label">Banda</div><div class="stat-value">${bandLabel(p.band)}</div></div>
            <div class="stat"><div class="stat-label">Canal / BW</div><div class="stat-value">${esc(p.channel ?? "—")} / ${esc(p.bandwidth_mhz ?? "—")} MHz</div></div>
            <div class="stat"><div class="stat-label">Bloques</div><div class="stat-value">${esc(p.accepted_blocks ?? "—")}</div></div>
            <div class="stat"><div class="stat-label">Muestras efectivas</div><div class="stat-value">${esc(p.effective_samples ?? "—")}</div></div>
          </div>
          ${p.note ? `<p class="muted">${esc(p.note)}</p>` : ""}
          <div class="actions">
            <button class="btn" data-action="validate-empty" data-id="${esc(p.id)}" ${canUse ? "" : "disabled"}>Validar vacío</button>
            <button class="btn btn-primary" data-action="validate-presence" data-id="${esc(p.id)}" ${canUse ? "" : "disabled"}>Validar presencia</button>
          </div>
        </div>
      </article>`;
  }

  async function refreshProfiles() {
    const data = await api("/api/calibrations");
    const root = $("cal-profiles");
    root.innerHTML = data.profiles.length
      ? data.profiles.map(profileCard).join("")
      : '<div class="alert">Aún no hay baselines guardados.</div>';
  }

  function renderJob(job) {
    const badge = $("cal-job-badge");
    if (!job || !["calibration", "calibration_validation", "ae_calibration"].includes(job.kind)) {
      badge.textContent = "Sin trabajo";
      badge.className = "badge";
      $("cal-job-message").textContent = "No hay calibración activa.";
      $("cal-progress").style.width = "0%";
      $("cal-block").textContent = "—";
      $("cal-state").textContent = "—";
      $("cal-guidance").style.display = "none";
      return;
    }

    badge.textContent = job.kind === "ae_calibration" ? "AE híbrido" : (job.kind === "calibration" ? "Calibración" : "Validación");
    badge.className = `badge ${job.state === "completed" ? "badge-success" : job.state === "failed" ? "badge-error" : "badge-info"}`;
    $("cal-job-message").textContent = job.message || job.state;
    $("cal-state").textContent = job.state;
    const block = Number(job.progress?.block || 0);
    const blocks = Number(job.progress?.blocks || job.target_blocks || 0);
    $("cal-block").textContent = blocks ? `${block} / ${blocks}` : String(block || "—");
    $("cal-progress").style.width = blocks ? `${Math.min(100, block / blocks * 100)}%` : "0%";

    const guide = $("cal-guidance");
    if (["queued", "countdown", "starting", "running"].includes(job.state)) {
      guide.style.display = "block";
      guide.textContent = job.kind === "ae_calibration"
        ? "Calibración AE: mantén la habitación completamente vacía. Al completar los bloques se recalibrarán los thresholds automáticamente."
        : job.kind === "calibration"
        ? "Mantén la habitación completamente vacía hasta que finalicen los 13 bloques."
        : (job.condition === "empty"
          ? "Validación EMPTY: deja la habitación vacía. El baseline no se modifica."
          : "Validación PRESENCIA: permanece dentro de la habitación. El baseline no se modifica.");
    } else {
      guide.style.display = "none";
    }
  }

  async function poll() {
    try {
      const job = await api("/api/jobs/current");
      renderJob(job);
      if (job && ["completed", "failed", "cancelled"].includes(job.state)) {
        await refreshProfiles();
        await refreshAeModel();
      }
    } catch (_) {}
  }

  async function refreshAeModel() {
    const data = await api("/api/ae/models");
    const band = $("ae-cal-band")?.value || "2.4g";
    const model = data.models?.[band];
    const badgeEl = $("ae-model-badge");
    if (!model?.ready) {
      if (badgeEl) { badgeEl.textContent = "No disponible"; badgeEl.className = "badge badge-warning"; }
      $("ae-model-id").textContent = model?.error || "—";
      $("ae-thr-spectral").textContent = "—";
      $("ae-thr-delta").textContent = "—";
      $("ae-model-calibrated").textContent = "—";
      return;
    }
    if (badgeEl) { badgeEl.textContent = "Listo"; badgeEl.className = "badge badge-success"; }
    $("ae-model-id").textContent = model.model_id || "—";
    $("ae-thr-spectral").textContent = Number(model.branches?.spectral?.effective_threshold || 0).toFixed(6);
    $("ae-thr-delta").textContent = Number(model.branches?.delta?.effective_threshold || 0).toFixed(6);
    $("ae-model-calibrated").textContent = model.calibration?.calibrated_at || "Sin calibración live";
  }

  $("ae-cal-band")?.addEventListener("change", () => refreshAeModel().catch(err => toast(err.message)));

  $("ae-cal-start")?.addEventListener("click", async () => {
    const button = $("ae-cal-start");
    button.disabled = true;
    try {
      await api("/api/ae/calibrate", {
        method: "POST",
        body: JSON.stringify({
          band: $("ae-cal-band").value,
          target_blocks: Number($("ae-cal-blocks").value),
          countdown_s: 5,
        }),
      });
      toast("Calibración AE iniciada. Deja la habitación vacía.");
      await poll();
    } catch (err) {
      toast(err.message);
    } finally {
      button.disabled = false;
    }
  });

  $("calibration-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("cal-start");
    button.disabled = true;
    try {
      await api("/api/calibrations/start", {
        method: "POST",
        body: JSON.stringify({
          name: $("cal-name").value,
          band: $("cal-band").value,
          note: $("cal-note").value,
          countdown_s: 5,
        }),
      });
      toast("Calibración iniciada. Sal de la habitación.");
      await poll();
    } catch (err) {
      toast(err.message);
    } finally {
      button.disabled = false;
    }
  });

  $("cal-profiles")?.addEventListener("click", async (event) => {
    const btn = event.target.closest("button[data-action]");
    if (!btn) return;
    const id = btn.dataset.id;
    const action = btn.dataset.action;
    btn.disabled = true;
    try {
      const condition = action === "validate-empty" ? "empty" : "presence_static";
      await api(`/api/calibrations/${encodeURIComponent(id)}/validate`, {
        method: "POST",
        body: JSON.stringify({condition, countdown_s: 5}),
      });
      toast(condition === "empty" ? "Validación EMPTY iniciada." : "Validación PRESENCIA iniciada.");
      await refreshProfiles();
      await poll();
    } catch (err) {
      toast(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  refreshProfiles().catch(err => toast(err.message));
  refreshAeModel().catch(err => toast(err.message));
  poll();
  setInterval(poll, 1500);
})();
