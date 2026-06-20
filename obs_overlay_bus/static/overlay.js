(function () {
  const root = document.getElementById("overlay-root");
  const DEFAULT_DURATION_MS = 2000;
  const LOAD_TIMEOUT_MS = 5000;

  let effects = {};
  const states = new Map();

  function log(...args) {
    console.info("[overlay]", ...args);
  }

  function warn(...args) {
    console.warn("[overlay]", ...args);
  }

  function getState(effectName) {
    if (!states.has(effectName)) {
      states.set(effectName, {
        isPlaying: false,
        lastPlayedAt: 0,
        queue: [],
        cooldownTimer: null,
        cancelCurrent: null,
        restartPending: false,
      });
    }
    return states.get(effectName);
  }

  function durationFor(config) {
    const duration = Number(config.duration_ms);
    return Number.isFinite(duration) && duration > 0 ? duration : DEFAULT_DURATION_MS;
  }

  function cooldownFor(config) {
    const cooldown = Number(config.cooldown_ms);
    return Number.isFinite(cooldown) && cooldown > 0 ? cooldown : 0;
  }

  function volumeFor(config) {
    const volume = Number(config.volume ?? 1);
    return Number.isFinite(volume) ? Math.max(0, Math.min(1, volume)) : 1;
  }

  function queuePolicyFor(config) {
    return config.queue_policy || "drop_if_busy";
  }

  function cooldownRemaining(config, state) {
    const elapsed = Date.now() - state.lastPlayedAt;
    return Math.max(0, cooldownFor(config) - elapsed);
  }

  function withCacheBust(src) {
    const separator = src.includes("?") ? "&" : "?";
    return `${src}${separator}overlay_ts=${Date.now()}`;
  }

  function applyPosition(element, position) {
    const pos = position || {};
    element.className = "overlay-effect";
    element.style.left = pos.left || "0px";
    element.style.top = pos.top || "0px";
    element.style.width = pos.width || "100%";
    element.style.height = pos.height || "100%";
  }

  async function loadEffects() {
    const response = await fetch("/effects", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`GET /effects failed with ${response.status}`);
    }

    const data = await response.json();
    effects = data.effects || {};
    Object.keys(effects).forEach(getState);
    log("loaded effects", Object.keys(effects));
  }

  function connectEvents() {
    const source = new EventSource("/events");

    source.addEventListener("ready", (event) => {
      log("event stream ready", JSON.parse(event.data));
    });

    source.addEventListener("trigger", (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.effect && data.config) {
          effects[data.effect] = data.config;
          getState(data.effect);
        }
        triggerEffect(data.effect, data.payload || {});
      } catch (error) {
        warn("invalid trigger event", error);
      }
    });

    source.addEventListener("config_reload", async (event) => {
      log("config reload event", JSON.parse(event.data));
      try {
        await loadEffects();
      } catch (error) {
        warn("failed to reload effects", error);
      }
    });

    source.onerror = () => {
      warn("event stream disconnected; browser will retry automatically");
    };
  }

  function triggerEffect(effectName, payload) {
    const config = effects[effectName];
    if (!config) {
      warn("unknown effect from event stream", effectName);
      return;
    }

    const state = getState(effectName);
    const policy = queuePolicyFor(config);

    if (state.isPlaying) {
      if (policy === "queue") {
        state.queue.push({ payload });
        log("queued effect", effectName, "queue length", state.queue.length);
      } else if (policy === "restart") {
        state.restartPending = true;
        state.queue.unshift({ payload });
        if (state.cancelCurrent) {
          state.cancelCurrent("restart");
        }
      } else {
        log("dropped busy effect", effectName);
      }
      return;
    }

    const wait = cooldownRemaining(config, state);
    if (wait > 0) {
      if (policy === "queue") {
        state.queue.push({ payload });
        scheduleDrain(effectName);
      } else {
        log("dropped cooling-down effect", effectName);
      }
      return;
    }

    playNow(effectName, payload);
  }

  function scheduleDrain(effectName) {
    const config = effects[effectName];
    const state = getState(effectName);
    if (!config || state.isPlaying || state.queue.length === 0) {
      return;
    }

    const wait = cooldownRemaining(config, state);
    window.clearTimeout(state.cooldownTimer);
    state.cooldownTimer = window.setTimeout(() => {
      const next = state.queue.shift();
      if (next) {
        playNow(effectName, next.payload);
      }
    }, wait);
  }

  async function playNow(effectName, payload) {
    const config = effects[effectName];
    const state = getState(effectName);
    window.clearTimeout(state.cooldownTimer);
    state.isPlaying = true;
    state.cancelCurrent = null;
    log("playing effect", effectName, payload);

    try {
      if (config.type === "video") {
        await playVideo(effectName, config, state);
      } else if (config.type === "image") {
        await playImage(effectName, config, state);
      } else {
        warn("effect type is registered but not implemented in this renderer", config.type);
      }
    } catch (error) {
      warn("effect playback failed", effectName, error);
    } finally {
      state.isPlaying = false;
      if (state.restartPending && state.queue.length > 0) {
        state.lastPlayedAt = 0;
        state.restartPending = false;
      } else {
        state.lastPlayedAt = Date.now();
      }
      state.cancelCurrent = null;
      scheduleDrain(effectName);
    }
  }

  function playVideo(effectName, config, state) {
    return new Promise((resolve) => {
      const video = document.createElement("video");
      let started = false;
      let finished = false;
      let loadTimer = null;
      let durationTimer = null;

      function finish(reason) {
        if (finished) {
          return;
        }
        finished = true;
        window.clearTimeout(loadTimer);
        window.clearTimeout(durationTimer);
        video.pause();
        video.removeAttribute("src");
        video.load();
        video.remove();
        log("video finished", effectName, reason);
        resolve();
      }

      state.cancelCurrent = finish;
      applyPosition(video, config.position);
      video.preload = "auto";
      video.playsInline = true;
      video.autoplay = false;
      video.controls = false;
      video.volume = volumeFor(config);
      video.muted = video.volume <= 0;

      video.addEventListener("error", () => finish("load_error"), { once: true });
      video.addEventListener("ended", () => finish("ended"), { once: true });
      video.addEventListener(
        "canplay",
        async () => {
          if (finished || started) {
            return;
          }
          started = true;
          window.clearTimeout(loadTimer);
          root.appendChild(video);
          try {
            video.currentTime = 0;
            await video.play();
            durationTimer = window.setTimeout(() => finish("duration"), durationFor(config));
          } catch (error) {
            warn("video play rejected", effectName, error);
            finish("play_rejected");
          }
        },
        { once: true },
      );

      loadTimer = window.setTimeout(() => finish("load_timeout"), LOAD_TIMEOUT_MS);
      video.src = withCacheBust(config.src);
      video.load();
    });
  }

  function playImage(effectName, config, state) {
    return new Promise((resolve) => {
      const image = document.createElement("img");
      let finished = false;
      let loadTimer = null;
      let durationTimer = null;

      function finish(reason) {
        if (finished) {
          return;
        }
        finished = true;
        window.clearTimeout(loadTimer);
        window.clearTimeout(durationTimer);
        image.remove();
        log("image finished", effectName, reason);
        resolve();
      }

      state.cancelCurrent = finish;
      applyPosition(image, config.position);
      image.alt = "";
      image.decoding = "async";

      image.addEventListener(
        "load",
        () => {
          if (finished) {
            return;
          }
          window.clearTimeout(loadTimer);
          root.appendChild(image);
          durationTimer = window.setTimeout(() => finish("duration"), durationFor(config));
        },
        { once: true },
      );
      image.addEventListener("error", () => finish("load_error"), { once: true });

      loadTimer = window.setTimeout(() => finish("load_timeout"), LOAD_TIMEOUT_MS);
      image.src = withCacheBust(config.src);
    });
  }

  async function boot() {
    try {
      await loadEffects();
      connectEvents();
    } catch (error) {
      warn("overlay failed to boot", error);
      window.setTimeout(boot, 2000);
    }
  }

  boot();
})();
