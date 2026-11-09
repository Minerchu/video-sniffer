(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    parsing: false,
    lastResult: null,
    statusTimer: null,
    toastTimer: null,
    jobTimers: new Map(),
  };

  const STATUS_MESSAGES = [
    "正在打开页面…",
    "正在监听播放器网络请求…",
    "正在解析媒体清单…",
    "正在整理可下载格式…",
  ];
  const KIND_LABELS = { video: "视频", audio: "音频", hls: "HLS 流", live: "直播" };
  const ENGINE_LABELS = { "yt-dlp": "站点解析", HTML: "网页源码", Playwright: "浏览器监听" };
  const HISTORY_KEY = "shipin.history.v1";
  const THEME_KEY = "shipin.theme.v1";

  const form = $("parse-form");
  const urlInput = $("url-input");
  const cookieInput = $("cookie-input");
  const refererInput = $("referer-input");
  const parseBtn = $("parse-btn");
  const parseLabel = parseBtn.querySelector(".btn-label");
  const spinner = parseBtn.querySelector(".spinner");
  const pasteBtn = $("paste-btn");
  const statusPanel = $("status-panel");
  const statusText = $("status-text");
  const results = $("results");
  const resultTitle = $("result-title");
  const resultSub = $("result-sub");
  const engineChips = $("engine-chips");
  const resultGrid = $("result-grid");
  const warningsBox = $("warnings");
  const warningList = $("warning-list");
  const warningCount = $("warning-count");
  const history = $("history");
  const historyList = $("history-list");
  const clearHistoryBtn = $("clear-history");
  const themeBtn = $("theme-btn");
  const toastNode = $("toast");
  const cardTemplate = $("card-template");

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function isHttpUrl(value) {
    try {
      const parsed = new URL(value);
      return parsed.protocol === "http:" || parsed.protocol === "https:";
    } catch (_) {
      return false;
    }
  }

  function normalizeUrl(raw) {
    let value = String(raw || "").trim();
    if (!value) return "";
    if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(value)) value = "https://" + value;
    return value;
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
    return value.replace(/[.,;:!?)\]}"'，。；：！？、】）]+$/g, "");
  }

  function hostnameOf(value) {
    try {
      return new URL(value).hostname.replace(/^www\./, "");
    } catch (_) {
      return "";
    }
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value >= 100 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return "";
    const total = Math.round(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const rest = total % 60;
    if (hours) return `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
    return `${minutes}:${String(rest).padStart(2, "0")}`;
  }

  function safeFilename(value, fallback) {
    const cleaned = String(value || "")
      .replace(/[<>:"/\\|?*\u0000-\u001f]/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/[. ]+$/g, "");
    return (cleaned || fallback || "media").slice(0, 100);
  }

  function filenameFor(item) {
    let base = item.title && item.title !== "未命名媒体" ? item.title : "";
    if (!base) {
      try {
        const path = new URL(item.url).pathname;
        base = decodeURIComponent(path.split("/").filter(Boolean).pop() || "media");
        base = base.replace(/\.[a-zA-Z0-9]{1,8}$/, "");
      } catch (_) {
        base = "media";
      }
    }
    const ext = item.ext || (item.kind === "audio" ? "mp3" : "mp4");
    return `${safeFilename(base, "media")}.${ext}`;
  }

  function showToast(message) {
    toastNode.textContent = message;
    toastNode.classList.add("show");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toastNode.classList.remove("show"), 3000);
  }

  function setStatus(message, isError = false) {
    statusText.textContent = message;
    statusPanel.hidden = false;
    statusPanel.classList.toggle("error", Boolean(isError));
  }

  function hideStatus() {
    statusPanel.hidden = true;
    statusPanel.classList.remove("error");
  }

  function setLoading(loading) {
    state.parsing = loading;
    parseBtn.disabled = loading;
    spinner.hidden = !loading;
    parseLabel.textContent = loading ? "解析中" : state.lastResult ? "重新解析" : "开始解析";
    clearInterval(state.statusTimer);
    if (!loading) return;
    let index = 0;
    setStatus(STATUS_MESSAGES[index]);
    state.statusTimer = setInterval(() => {
      index = (index + 1) % STATUS_MESSAGES.length;
      setStatus(STATUS_MESSAGES[index]);
    }, 1800);
  }

  async function readJson(response) {
    const text = await response.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_) {
        data = { detail: text };
      }
    }
    if (!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : `请求失败（HTTP ${response.status}）`;
      throw new Error(detail);
    }
    return data;
  }

  async function copyText(value) {
    const text = String(value || "");
    if (!text) return;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const area = document.createElement("textarea");
        area.value = text;
        area.setAttribute("readonly", "");
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        area.remove();
      }
      showToast("链接已复制");
    } catch (_) {
      showToast("复制失败，请手动选择链接");
    }
  }

  async function handleParse(event) {
    event.preventDefault();
    if (state.parsing) return;
    const value = normalizeUrl(extractUrl(urlInput.value));
    if (!value || !isHttpUrl(value)) {
      showToast("请先粘贴一个有效的网页链接");
      urlInput.focus();
      return;
    }
    urlInput.value = value;
    setLoading(true);
    try {
      const response = await fetch("/api/parse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: value,
          cookie: cookieInput.value.trim() || null,
          referer: refererInput.value.trim() || null,
        }),
      });
      const data = await readJson(response);
      state.lastResult = data;
      renderResults(data);
      rememberHistory(value, data.title);
      if (!data.items || data.items.length === 0) {
        setStatus("没有识别到可下载媒体，试试展开高级设置填写 Cookie。", true);
      } else {
        hideStatus();
        showToast(`发现 ${data.items.length} 个媒体资源`);
      }
    } catch (error) {
      results.hidden = true;
      setStatus(error && error.message ? error.message : "解析失败，请稍后重试", true);
      showToast("解析失败");
    } finally {
      setLoading(false);
    }
  }

  function renderResults(data) {
    const items = Array.isArray(data.items) ? data.items : [];
    results.hidden = false;
    resultTitle.textContent = data.title || (items.length ? `发现 ${items.length} 个媒体资源` : "没有识别到媒体");
    const host = hostnameOf(data.final_url || data.url);
    resultSub.textContent = `${items.length} 个资源 · ${(Number(data.elapsed_ms || 0) / 1000).toFixed(1)} 秒${host ? ` · ${host}` : ""}`;

    engineChips.replaceChildren();
    const engines = data.engines && typeof data.engines === "object" ? data.engines : {};
    Object.keys(engines).forEach((key) => {
      const status = engines[key];
      const chip = el("span", `engine-chip ${status === "ok" ? "ok" : status === "error" ? "error" : "disabled"}`);
      chip.textContent = `${ENGINE_LABELS[key] || key} · ${status === "ok" ? "已运行" : status === "error" ? "未命中" : "已关闭"}`;
      engineChips.appendChild(chip);
    });

    resultGrid.replaceChildren();
    if (!items.length) {
      const card = el("article", "media-card");
      const body = el("div", "card-body");
      body.appendChild(el("h3", "card-title", "没有识别到可下载媒体"));
      const note = el("p", "card-note", "可以尝试：确认链接在浏览器里能播放；展开高级设置填写 Cookie；换用手机分享页链接；或者稍后重试。受 DRM 保护的内容无法下载。");
      note.hidden = false;
      body.appendChild(note);
      card.appendChild(body);
      resultGrid.appendChild(card);
    } else {
      items.forEach((item, index) => resultGrid.appendChild(renderCard(item, index)));
    }

    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    warningsBox.hidden = warnings.length === 0;
    warningCount.textContent = warnings.length ? `(${warnings.length})` : "";
    warningList.replaceChildren();
    warnings.forEach((warning) => warningList.appendChild(el("li", "", warning)));

    setTimeout(() => results.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
  }
  function renderCard(item, index) {
    const fragment = cardTemplate.content.cloneNode(true);
    const card = fragment.querySelector(".media-card");
    const img = card.querySelector(".thumb img");
    const fallback = card.querySelector(".thumb-fallback");
    const kindBadge = card.querySelector(".kind-badge");
    const durationBadge = card.querySelector(".duration-badge");
    const title = card.querySelector(".card-title");
    const meta = card.querySelector(".card-meta");
    const note = card.querySelector(".card-note");
    const button = card.querySelector(".download-btn");
    const label = card.querySelector(".download-label");
    const copyBtn = card.querySelector(".copy-btn");
    const progress = card.querySelector(".job-progress");
    const bar = card.querySelector(".job-progress .bar i");
    const progressText = card.querySelector(".job-text");
    const fileLink = card.querySelector(".job-file");

    const thumbnail = item.thumbnail || (state.lastResult && state.lastResult.thumbnail);
    if (thumbnail && isHttpUrl(thumbnail)) {
      img.src = thumbnail;
      img.alt = "";
      fallback.hidden = true;
      img.addEventListener("error", () => {
        img.removeAttribute("src");
        fallback.hidden = false;
      });
    } else {
      fallback.hidden = false;
    }

    const kind = item.kind || "video";
    kindBadge.textContent = KIND_LABELS[kind] || "媒体";
    kindBadge.classList.add(kind);
    const duration = formatDuration(item.duration);
    if (duration) {
      durationBadge.textContent = duration;
      durationBadge.classList.add("show");
    }

    let displayTitle = item.title || "未命名媒体";
    if (displayTitle === "未命名媒体") {
      try {
        const last = decodeURIComponent(new URL(item.url).pathname.split("/").filter(Boolean).pop() || "");
        if (last && last !== "/") displayTitle = last;
      } catch (_) {}
    }
    title.textContent = displayTitle;
    title.title = displayTitle;

    const chips = [];
    if (item.format) chips.push(["", item.format]);
    if (item.quality) chips.push(["quality", item.quality]);
    if (item.width && item.height) chips.push(["", `${item.width}×${item.height}`]);
    else if (item.height) chips.push(["", `${item.height}P`]);
    if (item.size) chips.push(["", formatBytes(item.size)]);
    if (item.source) chips.push(["", item.source]);
    chips.forEach(([className, text]) => {
      if (text) meta.appendChild(el("span", `meta-chip ${className || ""}`.trim(), text));
    });

    if (item.note) {
      note.textContent = item.note;
      note.hidden = false;
    }

    const isJob = item.mode === "job";
    const jobLabel = item.kind === "hls" ? "合并下载 MP4" : item.kind === "audio" ? "下载 MP3" : "准备 MP4";
    label.textContent = isJob ? jobLabel : `下载 ${item.format || "文件"}`;
    if (isJob) button.dataset.mode = "job";

    const ui = { card, button, label, progress, bar, text: progressText, fileLink };
    button.addEventListener("click", () => {
      if (isJob || cookieInput.value.trim()) startJob(item, ui);
      else triggerProxyDownload(item);
    });
    copyBtn.addEventListener("click", () => copyText(item.url));

    return card;
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

  function triggerProxyDownload(item) {
    const params = new URLSearchParams();
    params.set("url", item.url);
    params.set("filename", filenameFor(item));
    if (item.referer) params.set("referer", item.referer);
    triggerDownload(`/api/download?${params.toString()}`, filenameFor(item));
    showToast("已开始下载");
  }

  async function startJob(item, ui) {
    if (state.jobTimers.has(item.id)) return;
    ui.button.disabled = true;
    ui.label.textContent = "创建任务…";
    ui.progress.hidden = false;
    ui.bar.style.width = "0%";
    ui.text.textContent = "正在创建下载任务…";
    ui.fileLink.hidden = true;

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
          cookie: cookieInput.value.trim() || null,
        }),
      });
      const job = await readJson(response);
      pollJob(job.id, item, ui, Date.now());
    } catch (error) {
      ui.button.disabled = false;
      ui.label.textContent = "重试";
      ui.text.textContent = error && error.message ? error.message : "创建任务失败";
      state.jobTimers.delete(item.id);
      showToast("创建下载任务失败");
    }
  }

  function pollJob(jobId, item, ui, startedAt) {
    const tick = async () => {
      if (Date.now() - startedAt > 45 * 60 * 1000) {
        finishJobError(ui, item, "任务超时，请稍后重试");
        return;
      }
      try {
        const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
        const job = await readJson(response);
        const percent = Math.max(0, Math.min(100, Math.round(Number(job.progress || 0) * 100)));
        ui.progress.hidden = false;
        ui.bar.style.width = `${percent}%`;

        if (job.status === "done") {
          state.jobTimers.delete(item.id);
          ui.bar.style.width = "100%";
          ui.text.textContent = "文件已生成";
          ui.button.disabled = false;
          ui.label.textContent = "重新生成";
          ui.fileLink.hidden = false;
          ui.fileLink.href = `/api/jobs/${encodeURIComponent(jobId)}/file`;
          ui.fileLink.download = job.filename || filenameFor(item);
          ui.fileLink.textContent = `下载 ${job.filename || filenameFor(item)}`;
          triggerDownload(ui.fileLink.href, ui.fileLink.download);
          showToast("文件已生成，正在开始下载");
          return;
        }

        if (job.status === "error") {
          throw new Error(job.error || "下载或合并失败");
        }
        if (job.status === "cancelled") {
          throw new Error("任务已取消");
        }

        ui.text.textContent = job.status === "queued" ? "排队中…" : percent > 0 ? `下载 / 合并中 ${percent}%` : "正在连接源站…";
        const timer = setTimeout(tick, 1300);
        state.jobTimers.set(item.id, timer);
      } catch (error) {
        finishJobError(ui, item, error && error.message ? error.message : "下载失败");
      }
    };
    tick();
  }

  function finishJobError(ui, item, message) {
    state.jobTimers.delete(item.id);
    ui.button.disabled = false;
    ui.label.textContent = "重试";
    ui.text.textContent = message;
    showToast("下载失败");
  }

  function readHistory() {
    try {
      const value = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      return Array.isArray(value) ? value.filter((entry) => entry && isHttpUrl(entry.url)).slice(0, 8) : [];
    } catch (_) {
      return [];
    }
  }

  function rememberHistory(url, title) {
    const entries = readHistory().filter((entry) => entry.url !== url);
    entries.unshift({ url, title: title || "", ts: Date.now() });
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(entries.slice(0, 8)));
    } catch (_) {}
    renderHistory();
  }

  function renderHistory() {
    const entries = readHistory();
    history.hidden = entries.length === 0;
    historyList.replaceChildren();
    entries.forEach((entry) => {
      const chip = el("button", "history-chip");
      chip.type = "button";
      const title = el("span", "", entry.title || hostnameOf(entry.url) || entry.url);
      title.title = entry.url;
      chip.appendChild(title);
      chip.appendChild(el("small", "", hostnameOf(entry.url)));
      chip.addEventListener("click", () => {
        urlInput.value = entry.url;
        form.requestSubmit();
      });
      historyList.appendChild(chip);
    });
  }

  function applyTheme(theme) {
    const value = theme === "light" ? "light" : "dark";
    document.body.dataset.theme = value;
    try {
      localStorage.setItem(THEME_KEY, value);
    } catch (_) {}
  }

  form.addEventListener("submit", handleParse);

  pasteBtn.addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        urlInput.value = text.trim();
        urlInput.focus();
        showToast("已粘贴链接");
      } else {
        urlInput.focus();
      }
    } catch (_) {
      urlInput.focus();
      showToast("浏览器未允许读取剪贴板，请长按输入框粘贴");
    }
  });

  themeBtn.addEventListener("click", () => {
    applyTheme(document.body.dataset.theme === "light" ? "dark" : "light");
  });

  clearHistoryBtn.addEventListener("click", () => {
    try {
      localStorage.removeItem(HISTORY_KEY);
    } catch (_) {}
    renderHistory();
    showToast("已清空最近记录");
  });

  applyTheme(localStorage.getItem(THEME_KEY) || "dark");
  renderHistory();

  const sharedUrl = new URLSearchParams(window.location.search).get("url");
  if (sharedUrl && isHttpUrl(sharedUrl)) {
    urlInput.value = sharedUrl;
    showToast("已填入分享链接，点击开始解析");
  }
})();
