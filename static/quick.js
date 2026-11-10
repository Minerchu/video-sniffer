(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const input = $("quick-input");
  const parseBtn = $("quick-parse");
  const parseLabel = parseBtn.querySelector(".btn-label");
  const spinner = parseBtn.querySelector(".spinner");
  const pasteBtn = $("quick-paste");
  const statusBox = $("quick-status");
  const statusText = $("quick-status-text");
  const results = $("quick-results");
  const list = $("quick-list");
  const title = $("quick-title");
  const sub = $("quick-sub");
  const toast = $("quick-toast");

  let busy = false;
  let autoTried = false;
  const timers = new Map();
  let toastTimer = null;

  function showStatus(text, kind) {
    statusBox.hidden = false;
    statusBox.classList.toggle("error", kind === "error");
    statusText.textContent = text;
  }

  function hideStatus() {
    statusBox.hidden = true;
  }

  function showToast(text) {
    toast.textContent = text;
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 2400);
  }

  function extractUrl(raw) {
    const text = String(raw || "").trim();
    if (!text) return "";
    const match =
      text.match(/https?:\/\/[^\s<>"']+/i) ||
      text.match(/(?:(?:www|m|v|vm|vt)\.)?(?:douyin|tiktok)\.com\/[^\s<>"']+/i);
    if (!match) return "";
    let value = match[0];
    if (!/^https?:\/\//i.test(value)) value = "https://" + value;
    return value.replace(/[.,;:!?)\]}"'?????????]+$/g, "");
  }

  function safeFilename(value, fallback) {
    const cleaned = String(value || "")
      .replace(/[<>:"/\\|?*\u0000-\u001f]/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/[. ]+$/g, "");
    return (cleaned || fallback || "media").slice(0, 90);
  }

  function filenameFor(item) {
    const ext = item.ext || (item.kind === "audio" ? "mp3" : "mp4");
    const name = safeFilename(item.title, "media");
    return name.toLowerCase().endsWith("." + ext) ? name : name + "." + ext;
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "";
    const units = ["B", "KB", "MB", "GB"];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return (value >= 100 || index === 0 ? Math.round(value) : value.toFixed(1)) + " " + units[index];
  }

  async function readJson(response) {
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : {};
    } catch (_) {
      throw new Error("?????????????");
    }
    if (!response.ok) throw new Error((data && data.detail) || "????");
    return data;
  }

  function triggerDownload(href, filename) {
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.rel = "noopener";
    if (filename) anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }

  function downloadProxy(item) {
    const params = new URLSearchParams();
    params.set("url", item.url);
    params.set("filename", filenameFor(item));
    if (item.referer) params.set("referer", item.referer);
    triggerDownload("/api/download?" + params.toString(), filenameFor(item));
    showToast("?????");
  }

  async function createJob(item, ui) {
    if (ui) {
      ui.btn.disabled = true;
      ui.progress.hidden = false;
      ui.progress.textContent = "???????";
    } else {
      showStatus("???????????");
    }
    try {
      const response = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: item.url,
          title: item.title || null,
          format_selector: item.format_selector || "best",
          kind: item.kind === "audio" ? "audio" : "video",
          referer: item.referer || null,
        }),
      });
      const job = await readJson(response);
      pollJob(job.id, item, ui, Date.now());
    } catch (error) {
      if (ui) {
        ui.btn.disabled = false;
        ui.progress.textContent = error.message || "??????";
      } else {
        showStatus(error.message || "??????", "error");
      }
    }
  }

  function pollJob(jobId, item, ui, startedAt) {
    if (timers.has(jobId)) return;
    const finish = (message, isError) => {
      timers.delete(jobId);
      if (ui) {
        ui.btn.disabled = false;
        ui.progress.hidden = false;
        ui.progress.textContent = message;
      } else {
        showStatus(message, isError ? "error" : undefined);
      }
    };
    const tick = async () => {
      if (Date.now() - startedAt > 30 * 60 * 1000) {
        finish("??????????", true);
        return;
      }
      try {
        const response = await fetch("/api/jobs/" + encodeURIComponent(jobId), { cache: "no-store" });
        const job = await readJson(response);
        const percent = Math.max(0, Math.min(100, Math.round(Number(job.progress || 0) * 100)));
        const text = job.status === "queued" ? "????" : percent > 0 ? "?? / ?? " + percent + "%" : "?????";
        if (ui) {
          ui.progress.hidden = false;
          ui.progress.textContent = text;
        } else {
          showStatus("?????? ? " + text);
        }
        if (job.status === "done") {
          timers.delete(jobId);
          triggerDownload("/api/jobs/" + encodeURIComponent(jobId) + "/file", job.filename || filenameFor(item));
          if (ui) {
            ui.btn.disabled = false;
            ui.btn.textContent = "????";
            ui.progress.textContent = "?????";
          } else {
            hideStatus();
          }
          showToast("??????????");
          return;
        }
        if (job.status === "error" || job.status === "cancelled") {
          throw new Error(job.error || "????");
        }
        const timer = setTimeout(tick, 1500);
        timers.set(jobId, timer);
      } catch (error) {
        finish(error.message || "????", true);
      }
    };
    tick();
  }

  function renderItem(item) {
    const card = document.createElement("article");
    card.className = "quick-item";

    const heading = document.createElement("h3");
    heading.textContent = item.title || item.format || "??";
    card.appendChild(heading);

    const meta = document.createElement("p");
    meta.className = "quick-item-meta";
    const bits = [item.format, item.quality, item.width && item.height ? item.width + "?" + item.height : "", item.size ? formatBytes(item.size) : ""].filter(Boolean);
    meta.textContent = bits.join(" ? ");
    card.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "quick-actions";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "parse-btn quick-dl";
    button.textContent = item.mode === "job" ? (item.kind === "audio" ? "?? MP3" : "?? MP4") : "?? " + (item.format || "??");
    const progress = document.createElement("span");
    progress.className = "quick-progress";
    progress.hidden = true;
    button.addEventListener("click", () => {
      if (item.mode === "job") createJob(item, { btn: button, progress: progress });
      else downloadProxy(item);
    });
    actions.appendChild(button);
    actions.appendChild(progress);
    card.appendChild(actions);
    return card;
  }

  function render(data) {
    results.hidden = false;
    title.textContent = data.title || "?????";
    const engines = data.engines ? Object.values(data.engines).filter(Boolean).join(" / ") : "";
    sub.textContent = data.items.length + " ??????" + (engines ? " ? " + engines : "");
    list.innerHTML = "";

    const ordered = data.items.slice().sort((a, b) => {
      if (a.kind === "video" && b.kind !== "video") return -1;
      if (b.kind === "video" && a.kind !== "video") return 1;
      return (b.height || 0) - (a.height || 0);
    });
    ordered.forEach((item) => list.appendChild(renderItem(item)));

    const best = ordered.find((item) => item.kind === "video") || ordered[0];
    if (best && !autoTried) {
      autoTried = true;
      setTimeout(() => {
        if (best.mode === "job") createJob(best, null);
        else downloadProxy(best);
      }, 260);
    }
  }

  async function startParse() {
    if (busy) return;
    const url = extractUrl(input.value);
    if (!url) {
      showStatus("????????????????????????", "error");
      return;
    }
    busy = true;
    autoTried = false;
    parseBtn.disabled = true;
    spinner.hidden = false;
    parseLabel.textContent = "????";
    showStatus("????????????????? 5?20 ??");
    try {
      const response = await fetch("/api/parse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url }),
      });
      const data = await readJson(response);
      render(data);
      hideStatus();
    } catch (error) {
      showStatus(error.message || "??????????", "error");
    } finally {
      busy = false;
      parseBtn.disabled = false;
      spinner.hidden = true;
      parseLabel.textContent = "????";
    }
  }

  parseBtn.addEventListener("click", startParse);
  pasteBtn.addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        input.value = text.trim();
        startParse();
      } else {
        input.focus();
      }
    } catch (_) {
      input.focus();
      showToast("??????????");
    }
  });
  input.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") startParse();
  });

  const params = new URLSearchParams(window.location.search);
  const shared = params.get("text") || params.get("url");
  if (shared) {
    input.value = shared;
    setTimeout(startParse, 350);
  }
})();
