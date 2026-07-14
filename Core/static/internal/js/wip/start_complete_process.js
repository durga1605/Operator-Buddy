"use strict";

(function () {
  var SCAN_IDLE_MS = 80;
  var MIN_SCAN_LENGTH = 1;
  var NUMERIC_IDLE_MS = 4000; // 4 s delay before auto-advancing count fields

  function onReady(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
      return;
    }
    fn();
  }

  function getCookie(name) {
    var match = document.cookie.match(
      new RegExp(
        "(?:^|; )" + name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "=([^;]*)",
      ),
    );
    return match ? decodeURIComponent(match[1]) : null;
  }

  function getCsrfToken() {
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input && input.value) return input.value;
    return getCookie("csrftoken") || "";
  }

  function postJson(url, payload) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCsrfToken(),
      },
      body: JSON.stringify(payload),
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) {
          throw new Error(data.error || "Request failed");
        }
        return data;
      });
    });
  }

  /**
   * Auto-commit scan fields after brief idle (no Enter required).
   * Scanners that suffix Enter still work via keydown handler.
   */
  function attachBarcodeScanner(input, onScan) {
    var idleTimer = null;
    var busy = false;
    var lastCommitted = "";

    function clearIdleTimer() {
      if (idleTimer) {
        clearTimeout(idleTimer);
        idleTimer = null;
      }
    }

    function commit() {
      clearIdleTimer();
      if (busy || !input || input.disabled) return;
      var value = input.value.trim();
      if (value.length < MIN_SCAN_LENGTH || value === lastCommitted) return;
      busy = true;
      lastCommitted = value;
      Promise.resolve(onScan())
        .catch(function () {
          lastCommitted = "";
        })
        .finally(function () {
          busy = false;
        });
    }

    function scheduleCommit() {
      clearIdleTimer();
      idleTimer = setTimeout(commit, SCAN_IDLE_MS);
    }

    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        commit();
        return;
      }
      scheduleCommit();
    });
    input.addEventListener("input", scheduleCommit);
    input.addEventListener("keyup", function (e) {
      if (e.key !== "Enter") scheduleCommit();
    });
    input.addEventListener("paste", scheduleCommit);
    input.addEventListener("focus", function () {
      input.classList.add("wip-scan-input--active");
      lastCommitted = "";
    });
    input.addEventListener("blur", function () {
      input.classList.remove("wip-scan-input--active");
      var value = input.value.trim();
      if (!busy && value.length >= MIN_SCAN_LENGTH && value !== lastCommitted) {
        commit();
      } else {
        clearIdleTimer();
      }
    });
  }

  onReady(function () {
    var form = document.getElementById("wip-form");
    if (!form) return;

    var urls = {
      workOrder: form.getAttribute("data-url-work-order"),
      operator: form.getAttribute("data-url-operator"),
      validate: form.getAttribute("data-url-validate"),
      submit: form.getAttribute("data-url-submit"),
      machineProcesses: form.getAttribute("data-url-machine-processes"),
    };

    var state = {
      woScanned: false,
      operatorScanned: false,
      machineValidated: false,
      processStarted: false,
      hasBalance: false, // true when partial qty already posted for this WO
      workOrder: "",
      materialCode: "",
      partName: "",
      partNo: "",
      altBom: "",
      machineId: "",
      processName: "",
      operatorId: "",
      sapLines: [],
      timerInterval: null,
      seconds: 0,
      inProgress: false,
      rejectionDetails: [],
    };

    function el(id) {
      return document.getElementById(id);
    }

    function setStatus(message, type) {
      var statusEl = el("wip-scan-status");
      if (!statusEl) return;
      statusEl.textContent = message || "";
      statusEl.classList.remove("is-success", "is-error");
      if (type === "success") statusEl.classList.add("is-success");
      else if (type === "error") statusEl.classList.add("is-error");
    }

    function enableInput(id, enable) {
      var field = el(id);
      if (field) field.disabled = !enable;
    }

    function focusInput(id) {
      var field = el(id);
      if (field && !field.disabled) {
        field.focus();
        if (field.select) field.select();
      }
    }

    function lockInput(id) {
      enableInput(id, false);
      var field = el(id);
      if (field) field.classList.remove("wip-scan-input--active");
    }

    function activateSection(sectionId) {
      var section = el(sectionId);
      if (section) {
        section.classList.remove("wip-card--disabled");
        section.classList.add("is-active");
      }
    }

    function fillProcessOptions(processes, selected) {
      var select = el("process_select");
      if (!select) return;
      select.innerHTML = '<option value="">Select process</option>';
      (processes || []).forEach(function (name) {
        var opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        if (name === selected) opt.selected = true;
        select.appendChild(opt);
      });
      enableInput("process_select", true);
    }

    function fillAltBomOptions(lines) {
      var select = el("alt_bom_select");
      if (!select) return;
      select.innerHTML = '<option value="">Standard BOM</option>';
      var altBoms = new Set();
      (lines || []).forEach(function (line) {
        if (line.alt_bom) altBoms.add(line.alt_bom);
      });
      altBoms.forEach(function (bom) {
        var opt = document.createElement("option");
        opt.value = bom;
        opt.textContent = bom;
        select.appendChild(opt);
      });
      enableInput("alt_bom_select", true);
    }

    /* ── Timer ─────────────────────────────────────────────── */

    function renderTimer(seconds) {
      var timerEl = el("timer");
      if (!timerEl) return;
      var hrs = Math.floor(seconds / 3600)
        .toString()
        .padStart(2, "0");
      var mins = Math.floor((seconds % 3600) / 60)
        .toString()
        .padStart(2, "0");
      var secs = (seconds % 60).toString().padStart(2, "0");
      timerEl.textContent = hrs + ":" + mins + ":" + secs;
    }

    function startTimer(fromSeconds) {
      if (state.timerInterval) clearInterval(state.timerInterval);
      state.seconds = fromSeconds || 0;
      renderTimer(state.seconds);
      state.timerInterval = setInterval(function () {
        state.seconds++;
        renderTimer(state.seconds);
      }, 1000);
    }

    function stopTimer() {
      if (state.timerInterval) {
        clearInterval(state.timerInterval);
        state.timerInterval = null;
      }
    }

    /* ── Resume PARTIAL (balance remaining from prior COMPLETED) ── */

    /**
     * Called when scan_work_order returns status="partial".
     * Pre-fills operator/machine/process from the last COMPLETED record,
     * shows the balance alert, and jumps straight to production/rejection inputs.
     * No operator/machine/process scanning required.
     */
    function resumePartial(d) {
      state.woScanned = true;
      state.operatorScanned = true;
      state.machineValidated = true;
      state.processStarted = true;
      state.inProgress = false;
      state.hasBalance = true;
      state.workOrder = d.work_order || "";
      state.operatorId = d.operator_id || "";
      state.machineId = d.machine_id || "";
      state.processName = d.process_name || "";
      state.materialCode = d.material_code || "";
      state.partNo = d.part_no || "";

      /* WO info panel */
      lockInput("work_order");
      el("display_part_no").textContent = state.partNo || "—";
      el("display_woqty").textContent = String(d.woqty != null ? d.woqty : "—");
      el("wo-info-panel").classList.remove("d-none");

      /* Balance alert */
      showBalanceAlert(
        Number(d.woqty) || 0,
        Number(d.completed_qty) || 0,
        Number(d.balance_qty) || 0,
      );

      /* Operator — show locked */
      activateSection("step-operator");
      el("operator_id").value = state.operatorId;
      lockInput("operator_id");

      /* Machine + process — show locked */
      activateSection("step-machine");
      el("machine_id").value = state.machineId;
      lockInput("machine_id");
      fillProcessOptions([state.processName], state.processName);
      enableInput("process_select", false);

      /* Jump straight to production inputs — no Start button */
      activateSection("step-production");
      lockInput("start-btn");
      enableInput("production_count", true);
      enableInput("rejected_count", true);
      enableInput("submit-btn", true);

      startTimer(0);
      setStatus(
        "Balance: " +
          d.balance_qty +
          " remaining of " +
          d.woqty +
          " (" +
          d.completed_qty +
          " posted) — enter counts",
        "success",
      );
      focusInput("production_count");
    }

    /* ── Resume IN_PROGRESS ─────────────────────────────────── */

    /**
     * Called when scan_work_order returns status="in_progress".
     * Pre-fills all details from the DB record and jumps straight
     * to the production/rejection inputs with the timer resumed.
     */
    function resumeInProgress(d) {
      state.woScanned = true;
      state.operatorScanned = true;
      state.machineValidated = true;
      state.processStarted = true;
      state.inProgress = true;
      state.workOrder = d.work_order || "";
      state.operatorId = d.operator_id || "";
      state.machineId = d.machine_id || "";
      state.processName = d.process_name || "";
      state.materialCode = d.material_code || "";
      state.partNo = d.part_no || "";

      /* Work order info panel */
      lockInput("work_order");
      el("display_part_no").textContent = state.partNo || "—";
      el("display_woqty").textContent = String(d.woqty != null ? d.woqty : "—");
      el("wo-info-panel").classList.remove("d-none");

      /* Operator section — show locked with filled value */
      activateSection("step-operator");
      el("operator_id").value = state.operatorId;
      lockInput("operator_id");

      /* Machine section — show locked with filled values */
      activateSection("step-machine");
      el("machine_id").value = state.machineId;
      lockInput("machine_id");

      /* Process dropdown — add single option and lock */
      fillProcessOptions([state.processName], state.processName);
      enableInput("process_select", false);

      /* Jump to production section */
      activateSection("step-production");
      enableInput("production_count", true);
      enableInput("rejected_count", true);
      enableInput("submit-btn", true);
      lockInput("start-btn");

      /* Resume timer from elapsed seconds already on the clock */
      startTimer(d.elapsed_seconds || 0);

      setStatus("Process in progress — enter counts and submit", "success");
      focusInput("production_count");
    }

    /* ── Balance alert ──────────────────────────────────────── */

    function showBalanceAlert(woqty, completedQty, balanceQty) {
      var alertEl = el("balance-alert");
      var textEl = el("balance-alert-text");
      var barEl = el("balance-progress-bar");
      var doneLabel = el("balance-completed-label");
      var remLabel = el("balance-remaining-label");
      if (!alertEl || completedQty <= 0) return;

      var pct = woqty > 0 ? Math.round((completedQty / woqty) * 100) : 0;
      textEl.textContent =
        "Partially completed: " +
        completedQty +
        " of " +
        woqty +
        " posted — " +
        balanceQty +
        " remaining.";
      barEl.style.width = pct + "%";
      doneLabel.textContent = completedQty + " completed";
      remLabel.textContent = balanceQty + " remaining";
      alertEl.classList.remove("d-none");
    }

    /* ── Work order scan ────────────────────────────────────── */

    function scanWorkOrder() {
      if (state.woScanned) return Promise.resolve();
      var wo = el("work_order").value.trim();
      if (!wo) return Promise.resolve();

      setStatus("Fetching from SAP…", "");
      return postJson(urls.workOrder, { work_order: wo })
        .then(function (data) {
          if (data.status === "in_progress") {
            resumeInProgress(data.in_progress_data);
            return;
          }

          if (data.status === "partial") {
            resumePartial(data.partial_data);
            return;
          }

          state.woScanned = true;
          state.workOrder = data.work_order;
          state.materialCode = data.material_code || "";
          state.partName = data.part_name || "";
          state.partNo = data.part_no || data.material_code || "";
          state.sapLines = data.sap_lines || [];
          state.hasBalance = false;

          lockInput("work_order");
          el("display_part_no").textContent = state.partNo || "—";
          el("display_woqty").textContent = String(data.woqty ?? "—");
          el("wo-info-panel").classList.remove("d-none");

          var firstLine = state.sapLines[0];
          if (firstLine) {
            el("display_ip_desc").textContent = firstLine.ip_description || "—";
            el("display_op_desc").textContent = firstLine.op_description || "—";
          }

          fillAltBomOptions(state.sapLines);
          fillProcessOptions(data.processes || []);
          activateSection("step-operator");
          enableInput("operator_id", true);
          setStatus("WO OK — scan operator", "success");
          focusInput("operator_id");
        })
        .catch(function (err) {
          state.woScanned = false;
          enableInput("work_order", true);
          el("work_order").value = "";
          setStatus(err.message, "error");
          focusInput("work_order");
          throw err;
        });
    }

    /* ── Operator scan ──────────────────────────────────────── */

    function scanOperator() {
      if (!state.woScanned || state.operatorScanned) return Promise.resolve();
      var opId = el("operator_id").value.trim();
      if (!opId) return Promise.resolve();

      setStatus("Checking operator…", "");
      return postJson(urls.operator, { operator_id: opId })
        .then(function () {
          state.operatorScanned = true;
          state.operatorId = opId;
          lockInput("operator_id");
          activateSection("step-machine");
          enableInput("machine_id", true);
          setStatus("Operator OK — scan machine & pick process", "success");
          focusInput("machine_id");
        })
        .catch(function (err) {
          setStatus(err.message, "error");
          focusInput("operator_id");
          throw err;
        });
    }

    /* ── Machine scan → fetch processes ────────────────────── */

    function onMachineScanned() {
      if (!state.operatorScanned || state.machineValidated)
        return Promise.resolve();
      var machineId = el("machine_id").value.trim();
      if (!machineId) return Promise.resolve();

      setStatus("Fetching processes…", "");
      return postJson(urls.machineProcesses, {
        machine_id: machineId,
        part_no: state.partNo,
      })
        .then(function (data) {
          fillProcessOptions(data.processes || []);
          setStatus("Select process", "");
          focusInput("process_select");
        })
        .catch(function (err) {
          setStatus(err.message, "error");
          focusInput("machine_id");
          throw err;
        });
    }

    /* ── Validate machine + process ─────────────────────────── */

    function validateMachineProcess() {
      if (!state.operatorScanned || state.machineValidated)
        return Promise.resolve();
      var machineId = el("machine_id").value.trim();
      var processName = el("process_select").value.trim();
      if (!machineId) return Promise.resolve();
      if (!processName) {
        setStatus("Select process first", "error");
        focusInput("process_select");
        return Promise.resolve();
      }

      setStatus("Validating machine & process…", "");
      return postJson(urls.validate, {
        work_order: state.workOrder,
        machine_id: machineId,
        process_name: processName,
      })
        .then(function (data) {
          state.machineValidated = true;
          state.machineId = machineId;
          state.processName = processName;
          state.altBom = el("alt_bom_select")
            ? el("alt_bom_select").value || ""
            : "";
          lockInput("machine_id");
          enableInput("process_select", false);
          activateSection("step-production");

          var balanceQty = Number(data.balance_qty) || 0;
          var stockQty = Number(data.stock_qty) || 0;
          var completedQty = Number(data.completed_qty) || 0;

          if (state.hasBalance && balanceQty > 0) {
            /* ── Partial balance: skip Start, go straight to counts ── */
            state.processStarted = true;
            lockInput("start-btn");
            enableInput("production_count", true);
            enableInput("rejected_count", true);
            enableInput("submit-btn", true);
            startTimer(0);
            setStatus(
              "Balance " +
                balanceQty +
                " remaining (of " +
                stockQty +
                ", " +
                completedQty +
                " posted) — enter counts",
              "success",
            );
            focusInput("production_count");
          } else {
            /* ── Fresh start: show the Start button ── */
            enableInput("start-btn", true);
            setStatus("Valid — press Start", "success");
            focusInput("start-btn");
          }
        })
        .catch(function (err) {
          state.machineValidated = false;
          enableInput("start-btn", false);
          enableInput("production_count", false);
          enableInput("rejected_count", false);
          enableInput("submit-btn", false);
          setStatus(err.message, "error");
          if (err.message && err.message.indexOf("SAP") !== -1) {
            focusInput("process_select");
          } else {
            el("machine_id").value = "";
            focusInput("machine_id");
          }
          throw err;
        });
    }

    /* ── Start button ───────────────────────────────────────── */

    el("start-btn").addEventListener("click", function () {
      if (!state.machineValidated || state.processStarted) return;

      var payload = {
        mode: "START",
        work_order: state.workOrder,
        machine_id: state.machineId,
        process_name: state.processName,
        operator_id: state.operatorId,
      };

      setStatus("Starting process…", "");
      enableInput("start-btn", false);

      postJson(urls.submit, payload)
        .then(function () {
          state.processStarted = true;

          /* Start button clicked — redirect to home */
          window.location.href = "/mobility/";
        })
        .catch(function (err) {
          setStatus(err.message, "error");
          enableInput("start-btn", true);
        });
    });

    /* ── Process select change ──────────────────────────────── */

    el("process_select").addEventListener("change", function () {
      state.machineValidated = false;
      enableInput("start-btn", false);
      enableInput("production_count", false);
      enableInput("rejected_count", false);
      enableInput("submit-btn", false);
      var processName = el("process_select").value.trim();
      if (!processName) return;
      validateMachineProcess();
    });

    /* ── Numeric field: advance on idle ────────────────────── */

    function attachNumericAdvance(input, nextId) {
      if (!input) return;
      var idleTimer = null;
      function goNext() {
        if (input.disabled || !input.value.trim()) return;
        focusInput(nextId);
      }
      function schedule() {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(goNext, NUMERIC_IDLE_MS);
      }
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          e.preventDefault();
          goNext();
        }
      });
      input.addEventListener("input", schedule);
      input.addEventListener("keyup", function (e) {
        if (e.key !== "Enter") schedule();
      });
    }

    attachNumericAdvance(el("production_count"), "rejected_count");

    /* ── Submit ─────────────────────────────────────────────── */

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!state.processStarted) {
        setStatus("Press Start first", "error");
        return;
      }

      var rejectedCount = parseInt(el("rejected_count").value || "0", 10);
      if (rejectedCount > 0 && state.rejectionDetails.length === 0) {
        openRejectionModal();
        return;
      }

      setStatus("Submitting…", "");
      enableInput("submit-btn", false);

      var payload = {
        work_order: state.workOrder,
        operator_id: state.operatorId,
        machine_id: state.machineId,
        process_name: state.processName,
        alt_bom: state.altBom || "",
        material_code: state.materialCode,
        part_name: state.partName,
        production_count: el("production_count").value,
        rejected_count: el("rejected_count").value,
        rejection_details: state.rejectionDetails,
        process_started: true,
      };

      postJson(urls.submit, payload)
        .then(function (data) {
          stopTimer();
          window.alert(data.message || "Saved");
          if (data.status === "success") {
            window.location.reload();
          }
        })
        .catch(function (err) {
          setStatus(err.message, "error");
          enableInput("submit-btn", true);
        });
    });

    /* ── Rejection modal ─────────────────────────────────────── */

    function openRejectionModal() {
      var rejectedInput = el("rejected_count");
      if (!rejectedInput) return;
      var rejectedVal = parseInt(rejectedInput.value || "0", 10);
      if (isNaN(rejectedVal) || rejectedVal <= 0) return;

      el("modal-rejected-count").textContent = rejectedVal;
      state.rejectionDetails = [];
      renderRejectionList();
      var modal = new bootstrap.Modal(el("rejectionModal"));
      modal.show();
      el("rejection-reason").focus();
    }

    function closeRejectionModal() {
      var modalEl = el("rejectionModal");
      if (!modalEl) return;
      var modal = bootstrap.Modal.getInstance(modalEl);
      if (modal) modal.hide();
    }

    function renderRejectionList() {
      var listEl = el("rejection-list");
      if (!listEl) return;
      listEl.innerHTML = "";
      var total = 0;
      state.rejectionDetails.forEach(function (item, index) {
        total += item.count;
        var div = document.createElement("div");
        div.className = "rejection-item";
        div.innerHTML =
          '<span class="rejection-item-reason">' +
          item.reason +
          "</span>" +
          "<span>" +
          '<span class="rejection-item-count">' +
          item.count +
          "</span>" +
          '<button type="button" class="rejection-item-remove" data-index="' +
          index +
          '" aria-label="Remove">&times;</button>' +
          "</span>";
        listEl.appendChild(div);
      });
      el("rejection-total").textContent = total;
    }

    el("rejected_count").addEventListener("input", function () {
      var val = parseInt(this.value || "0", 10);
      var modalCountEl = el("modal-rejected-count");
      if (modalCountEl) modalCountEl.textContent = val;
      if (val > 0 && !el("rejectionModal").classList.contains("show")) {
        openRejectionModal();
      } else if (val <= 0) {
        state.rejectionDetails = [];
        renderRejectionList();
      }
    });

    el("add-reason-btn").addEventListener("click", function () {
      var reason = el("rejection-reason").value;
      var count = parseInt(el("rejection-reason-count").value || "0", 10);
      if (!reason) {
        setStatus("Select a reason first", "error");
        return;
      }
      if (isNaN(count) || count <= 0) {
        setStatus("Enter valid count", "error");
        return;
      }
      var rejectedInput = el("rejected_count");
      var rejectedVal = parseInt(rejectedInput.value || "0", 10);
      var currentTotal = state.rejectionDetails.reduce(function (sum, item) {
        return sum + item.count;
      }, 0);
      if (currentTotal + count > rejectedVal) {
        setStatus("Total exceeds rejected count", "error");
        return;
      }
      state.rejectionDetails.push({ reason: reason, count: count });
      renderRejectionList();
      el("rejection-reason").selectedIndex = 0;
      el("rejection-reason-count").value = "1";
      el("rejection-reason").focus();
    });

    el("rejection-list").addEventListener("click", function (e) {
      if (e.target.classList.contains("rejection-item-remove")) {
        var index = parseInt(e.target.getAttribute("data-index"), 10);
        state.rejectionDetails.splice(index, 1);
        renderRejectionList();
      }
    });

    el("save-rejection-btn").addEventListener("click", function () {
      closeRejectionModal();
    });

    /* ── Barcode attachments ────────────────────────────────── */

    attachBarcodeScanner(el("work_order"), scanWorkOrder);
    attachBarcodeScanner(el("operator_id"), scanOperator);
    attachBarcodeScanner(el("machine_id"), onMachineScanned);

    focusInput("work_order");
    setStatus("Scan work order", "");
  });
})();
