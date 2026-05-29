"use strict";

(function () {
  var STORAGE_KEY = "operatorBuddy.rememberUsername";

  function onReady(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
      return;
    }
    fn();
  }

  function canUseStorage() {
    try {
      var k = STORAGE_KEY + ".test";
      window.localStorage.setItem(k, "1");
      window.localStorage.removeItem(k);
      return true;
    } catch (storageError) {
      return false;
    }
  }

  function prefersReducedMotion() {
    return (
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  /* ---- Shopfloor traceability canvas (full page) ---- */
  function initTraceCanvas() {
    var canvas = document.getElementById("traceCanvas");
    if (!canvas) {
      return null;
    }

    var ctx = canvas.getContext("2d");
    if (!ctx) {
      return null;
    }

    var stations = [];
    var parts = [];
    var pulses = [];
    var w = 0;
    var h = 0;
    var animId = 0;
    var running = false;
    var reduced = prefersReducedMotion();

    var PART_IDS = ["P-AX4412", "P-BK9021", "P-VR1180", "P-HD3305", "P-LM7720"];
    var STATION_LABELS = ["RAW", "MACH", "QC", "ASSY", "PACK", "SHIP"];

    function resize() {
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      buildStations();
    }

    function buildStations() {
      stations = [];
      var count = w < 640 ? 4 : w < 1024 ? 5 : 6;
      var marginX = w * 0.08;
      var span = w - marginX * 2;
      var baseY = h * (w < 768 ? 0.55 : 0.62);

      for (var i = 0; i < count; i++) {
        var t = count === 1 ? 0.5 : i / (count - 1);
        var wave = Math.sin(t * Math.PI) * (h * 0.06);
        stations.push({
          x: marginX + span * t,
          y: baseY - wave,
          label: STATION_LABELS[i] || "STN",
          pulse: Math.random() * Math.PI * 2,
        });
      }
    }

    function spawnPart() {
      if (stations.length < 2 || parts.length > (reduced ? 4 : 14)) {
        return;
      }
      parts.push({
        from: 0,
        to: 1,
        t: 0,
        speed: 0.003 + Math.random() * 0.004,
        id: PART_IDS[Math.floor(Math.random() * PART_IDS.length)],
        color: Math.random() > 0.5 ? "#38bdf8" : "#2dd4bf",
      });
    }

    function addPulse(x, y) {
      pulses.push({ x: x, y: y, r: 0, a: 0.6 });
    }

    function drawGrid() {
      ctx.strokeStyle = "rgba(56, 189, 248, 0.04)";
      ctx.lineWidth = 1;
      var step = 56;
      for (var x = 0; x < w; x += step) {
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, h);
        ctx.stroke();
      }
      for (var y = 0; y < h; y += step) {
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
      }
    }

    function drawConveyor() {
      if (stations.length < 2) {
        return;
      }
      var first = stations[0];
      var last = stations[stations.length - 1];
      var y = (first.y + last.y) / 2;

      ctx.strokeStyle = "rgba(56, 189, 248, 0.2)";
      ctx.lineWidth = 2;
      ctx.setLineDash([10, 14]);
      ctx.lineDashOffset = -((Date.now() / 40) % 24);
      ctx.beginPath();
      ctx.moveTo(first.x, y);
      ctx.lineTo(last.x, y);
      ctx.stroke();
      ctx.setLineDash([]);

      var seg = 28;
      var offset = (Date.now() / 35) % (seg * 2);
      for (var x = first.x; x < last.x; x += seg) {
        var alpha = 0.15 + 0.1 * Math.sin((x + offset) * 0.05);
        ctx.fillStyle = "rgba(56, 189, 248, " + alpha + ")";
        ctx.fillRect(x - 2, y - 1, 8, 2);
      }
    }

    function drawStations() {
      stations.forEach(function (st, i) {
        st.pulse += reduced ? 0 : 0.02;
        var glow = 0.35 + 0.25 * Math.sin(st.pulse);

        ctx.beginPath();
        ctx.arc(st.x, st.y, 22, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(15, 23, 42, 0.85)";
        ctx.fill();
        ctx.strokeStyle = "rgba(56, 189, 248, " + (0.35 + glow * 0.4) + ")";
        ctx.lineWidth = 2;
        ctx.stroke();

        ctx.beginPath();
        ctx.arc(st.x, st.y, 6, 0, Math.PI * 2);
        ctx.fillStyle = i % 2 === 0 ? "#38bdf8" : "#2dd4bf";
        ctx.fill();

        ctx.font = "600 10px ui-monospace, Consolas, monospace";
        ctx.fillStyle = "rgba(148, 163, 184, 0.75)";
        ctx.textAlign = "center";
        ctx.fillText(st.label, st.x, st.y + 36);
      });
    }

    function drawConnections() {
      ctx.strokeStyle = "rgba(45, 212, 191, 0.12)";
      ctx.lineWidth = 1;
      for (var i = 0; i < stations.length - 1; i++) {
        ctx.beginPath();
        ctx.moveTo(stations[i].x, stations[i].y);
        ctx.lineTo(stations[i + 1].x, stations[i + 1].y);
        ctx.stroke();
      }
    }

    function drawParts() {
      parts.forEach(function (p) {
        if (!reduced) {
          p.t += p.speed;
        }
        if (p.t >= 1) {
          p.from = p.to;
          p.to = Math.min(p.from + 1, stations.length - 1);
          p.t = 0;
          if (p.from >= stations.length - 1) {
            p.done = true;
            addPulse(stations[p.to].x, stations[p.to].y);
          }
        }
        if (p.done) {
          return;
        }

        var a = stations[p.from];
        var b = stations[p.to];
        if (!a || !b) {
          return;
        }

        var x = a.x + (b.x - a.x) * p.t;
        var y = a.y + (b.y - a.y) * p.t - 8;

        ctx.fillStyle = p.color;
        ctx.shadowColor = p.color;
        ctx.shadowBlur = 12;
        ctx.fillRect(x - 5, y - 5, 10, 10);
        ctx.shadowBlur = 0;

        ctx.font = "500 9px ui-monospace, Consolas, monospace";
        ctx.fillStyle = "rgba(125, 211, 252, 0.5)";
        ctx.textAlign = "center";
        ctx.fillText(p.id, x, y - 12);
      });
      parts = parts.filter(function (p) {
        return !p.done;
      });
    }

    function drawPulses() {
      pulses.forEach(function (p) {
        p.r += reduced ? 0 : 1.2;
        p.a -= 0.018;
        if (p.a <= 0) {
          p.dead = true;
          return;
        }
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.strokeStyle = "rgba(56, 189, 248, " + p.a + ")";
        ctx.lineWidth = 2;
        ctx.stroke();
      });
      pulses = pulses.filter(function (p) {
        return !p.dead;
      });
    }

    function drawScanLine() {
      if (reduced) {
        return;
      }
      var scanY = (Math.sin(Date.now() / 1200) * 0.5 + 0.5) * h;
      var grad = ctx.createLinearGradient(0, scanY - 30, 0, scanY + 30);
      grad.addColorStop(0, "transparent");
      grad.addColorStop(0.5, "rgba(56, 189, 248, 0.06)");
      grad.addColorStop(1, "transparent");
      ctx.fillStyle = grad;
      ctx.fillRect(0, scanY - 30, w, 60);
    }

    function paintStatic() {
      ctx.clearRect(0, 0, w, h);
      drawGrid();
      drawConnections();
      drawConveyor();
      drawStations();
    }

    function frame() {
      if (!running) {
        return;
      }
      ctx.clearRect(0, 0, w, h);
      drawGrid();
      drawConnections();
      drawConveyor();
      drawStations();
      drawParts();
      drawPulses();
      drawScanLine();

      if (!reduced && Math.random() < 0.025) {
        spawnPart();
      }

      animId = requestAnimationFrame(frame);
    }

    function start() {
      resize();
      if (reduced) {
        paintStatic();
        return;
      }
      running = true;
      for (var i = 0; i < 5; i++) {
        spawnPart();
      }
      frame();
    }

    function stop() {
      running = false;
      if (animId) {
        cancelAnimationFrame(animId);
        animId = 0;
      }
    }

    window.addEventListener("resize", function () {
      resize();
      if (reduced) {
        paintStatic();
      }
    });

    document.addEventListener("visibilitychange", function () {
      if (reduced) {
        return;
      }
      if (document.hidden) {
        stop();
      } else if (!running) {
        start();
      }
    });

    start();
    return { stop: stop };
  }

  /* ---- Rotating HUD tags ---- */
  function initHudTags() {
    var hud = document.querySelector(".login-trace-hud[data-hud-labels]");
    var tags = document.querySelectorAll(".login-trace-tag[data-tag]");
    if (!hud || !tags.length || prefersReducedMotion()) {
      return;
    }

    var pool = hud.getAttribute("data-hud-labels").split(",");
    if (!pool.length) {
      return;
    }

    setInterval(function () {
      tags.forEach(function (el) {
        if (Math.random() > 0.7) {
          el.textContent = pool[Math.floor(Math.random() * pool.length)];
        }
      });
    }, 3200);
  }

  /* ---- Form behaviour ---- */
  function initForm() {
    var form = document.getElementById("loginForm");
    var usernameInput = document.getElementById("username");
    var passwordInput = document.getElementById("password");
    var toggleBtn = document.getElementById("togglePassword");
    var submitBtn = document.getElementById("loginSubmit");
    var storageOk = canUseStorage();

    if (!form) {
      return;
    }

    if (storageOk && usernameInput) {
      var saved = window.localStorage.getItem(STORAGE_KEY);
      if (saved && !usernameInput.value) {
        usernameInput.value = saved;
      }
    }

    if (toggleBtn && passwordInput) {
      toggleBtn.addEventListener("click", function () {
        var show = passwordInput.type === "password";
        passwordInput.type = show ? "text" : "password";
        toggleBtn.setAttribute("aria-pressed", String(show));
        toggleBtn.setAttribute(
          "aria-label",
          show ? "Hide password" : "Show password",
        );
        var icon = toggleBtn.querySelector(".bi");
        if (icon) {
          icon.classList.toggle("bi-eye", !show);
          icon.classList.toggle("bi-eye-slash", show);
        }
      });
    }

    form.addEventListener("submit", function () {
      if (storageOk && usernameInput && usernameInput.value.trim()) {
        window.localStorage.setItem(STORAGE_KEY, usernameInput.value.trim());
      }

      if (!form.checkValidity()) {
        return;
      }

      if (submitBtn) {
        submitBtn.disabled = true;
        var label = submitBtn.querySelector(".login-submit__label");
        var spinner = submitBtn.querySelector(".login-submit__spinner");
        if (label) {
          label.classList.add("d-none");
        }
        if (spinner) {
          spinner.classList.remove("d-none");
        }
      }
    });
  }

  onReady(function () {
    initTraceCanvas();
    initHudTags();
    initForm();
  });
})();
