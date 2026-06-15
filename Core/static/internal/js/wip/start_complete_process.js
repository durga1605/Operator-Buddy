"use strict";

/**
 * WIP flow: Work Order → SAP details → Operator → Machine + Process → Start → Submit
 */

(function () {
  var SCAN_IDLE_MS = 80;
  var MIN_SCAN_LENGTH = 1;

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
    if (input && input.value) {
      return input.value;
    }
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
      if (busy || !input || input.disabled) {
        return;
      }
      var value = input.value.trim();
      if (value.length < MIN_SCAN_LENGTH || value === lastCommitted) {
        return;
      }
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

    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        commit();
        return;
      }
      scheduleCommit();
    });

    input.addEventListener("input", scheduleCommit);

    input.addEventListener("keyup", function (event) {
      if (event.key !== "Enter") {
        scheduleCommit();
      }
    });

    input.addEventListener("paste", function () {
      scheduleCommit();
    });

    input.addEventListener("focus", function () {
      input.classList.add("wip-scan-input--active");
      lastCommitted = "";
    });

    input.addEventListener("blur", function () {
      input.classList.remove("wip-scan-input--active");
      // Commit on blur if there's uncommitted value (scanner may move focus before idle timer fires)
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
    if (!form) {
      return;
    }

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
      inProgressData: null,
    };

    function el(id) {
      return document.getElementById(id);
    }

    function setStatus(message, type) {
      var statusEl = el("wip-scan-status");
      if (!statusEl) {
        return;
      }
      statusEl.textContent = message || "";
      statusEl.classList.remove("is-success", "is-error");
      if (type === "success") {
        statusEl.classList.add("is-success");
      } else if (type === "error") {
        statusEl.classList.add("is-error");
      }
    }

    function enableInput(id, enable) {
      var field = el(id);
      if (field) {
        field.disabled = !enable;
      }
    }

    function focusInput(id) {
      var field = el(id);
      if (field && !field.disabled) {
        field.focus();
        if (field.select) {
          field.select();
        }
      }
    }

    function lockInput(id) {
      enableInput(id, false);
      var field = el(id);
      if (field) {
        field.classList.remove("wip-scan-input--active");
      }
    }

    function activateSection(sectionId) {
      var section = el(sectionId);
      if (section) {
        section.classList.remove("wip-card--disabled");
        section.classList.add("is-active");
      }
    }

    function fillProcessOptions(processes) {
      var select = el("process_select");
      if (!select) {
        return;
      }
      select.innerHTML = '<option value="">Select process</option>';
      (processes || []).forEach(function (name) {
        var opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        select.appendChild(opt);
      });
      enableInput("process_select", true);
    }

    function fillAltBomOptions(lines) {
      var select = el("alt_bom_select");
      if (!select) {
        return;
      }
      select.innerHTML = '<option value="">Standard BOM</option>';
      var altBoms = new Set();
      (lines || []).forEach(function (line) {
        if (line.alt_bom) {
          altBoms.add(line.alt_bom);
        }
      });
      altBoms.forEach(function (bom) {
        var opt = document.createElement("option");
        opt.value = bom;
        opt.textContent = bom;
        select.appendChild(opt);
      });
      enableInput("alt_bom_select", true);
    }

    function startTimer() {
      if (state.timerInterval) {
        clearInterval(state.timerInterval);
      }
      state.seconds = 0;
      state.timerInterval = setInterval(function () {
        state.seconds++;
        var hrs = Math.floor(state.seconds / 3600)
          .toString()
          .padStart(2, "0");
        var mins = Math.floor((state.seconds % 3600) / 60)
          .toString()
          .padStart(2, "0");
        var secs = (state.seconds % 60).toString().padStart(2, "0");
        var timerEl = el("timer");
        if (timerEl) {
          timerEl.textContent = hrs + ":" + mins + ":" + secs;
        }
      }, 1000);
    }

    function scanWorkOrder() {
      if (state.woScanned) {
        return Promise.resolve();
      }

      var wo = el("work_order").value.trim();
      if (!wo) {
        return Promise.resolve();
      }
      setStatus("Fetching from SAP…", "");
      return postJson(urls.workOrder, { work_order: wo })
        .then(function (data) {
          if (data.status === "in_progress") {
            state.woScanned = true;
            state.inProgress = true;
            state.inProgressData = data.in_progress_data;
            state.workOrder = data.in_progress_data.work_order;
            state.machineId = data.in_progress_data.machine_id;
            state.processName = data.in_progress_data.process_name;
            state.operatorId = data.in_progress_data.operator_id;
            state.materialCode = data.in_progress_data.material_code;
            state.partName = data.in_progress_data.part_name;
            state.partNo = data.in_progress_data.part_no;

            lockInput("work_order");
            el("display_part_no").textContent = state.partNo || "—";
            el("display_woqty").textContent = String(
              data.in_progress_data.woqty ?? "—",
            );
            el("wo-info-panel").classList.remove("d-none");

            // Skip to production
            activateSection("step-production");
            enableInput("production_count", true);
            enableInput("rejected_count", true);
            enableInput("submit-btn", true);

            // Auto-fill required fields
            el("machine_id").value = state.machineId || "";
            el("process_select").value = state.processName || "";

            state.processStarted = true;
            startTimer();
            setStatus("Process in progress — enter counts", "success");
            focusInput("production_count");
            return;
          }

          state.woScanned = true;
          state.workOrder = data.work_order;
          state.materialCode = data.material_code || "";
          state.partName = data.part_name || "";
          state.partNo = data.part_no || data.material_code || "";
          state.sapLines = data.sap_lines || [];

          lockInput("work_order");
          el("display_part_no").textContent = state.partNo || "—";
          el("display_woqty").textContent = String(data.woqty ?? "—");
          el("wo-info-panel").classList.remove("d-none");

          // Set descriptions
          var firstLine = data.sap_lines[0];
          if (firstLine) {
            el("display_ip_desc").textContent = firstLine.ip_description || "—";
            el("display_op_desc").textContent = firstLine.op_description || "—";
          }

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

    function scanOperator() {
      if (!state.woScanned || state.operatorScanned) {
        return Promise.resolve();
      }
      var opId = el("operator_id").value.trim();
      if (!opId) {
        return Promise.resolve();
      }
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

    function validateMachineProcess() {
      if (!state.operatorScanned || state.machineValidated) {
        return Promise.resolve();
      }
      var machineId = el("machine_id").value.trim();
      var processName = el("process_select").value.trim();
      if (!machineId) {
        return Promise.resolve();
      }
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
        .then(function () {
          state.machineValidated = true;
          state.machineId = machineId;
          state.processName = processName;
          lockInput("machine_id");
          enableInput("process_select", false);
          activateSection("step-production");
          enableInput("start-btn", true);
          setStatus("Valid — press Start", "success");
          focusInput("start-btn");
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
          } else if (err.message && err.message.indexOf("completed") !== -1) {
            el("machine_id").value = "";
            focusInput("machine_id");
          } else {
            focusInput("machine_id");
          }
          throw err;
        });
    }

    function onMachineScanned() {
      if (!state.operatorScanned || state.machineValidated) {
        return Promise.resolve();
      }
      var machineId = el("machine_id").value.trim();
      if (!machineId) {
        return Promise.resolve();
      }

      // Fetch processes from PMS_oee_cell for this part + machine
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

    attachBarcodeScanner(el("work_order"), scanWorkOrder);
    attachBarcodeScanner(el("operator_id"), scanOperator);
    attachBarcodeScanner(el("machine_id"), onMachineScanned);

    el("process_select").addEventListener("change", function () {
      state.machineValidated = false;
      enableInput("start-btn", false);
      enableInput("production_count", false);
      enableInput("rejected_count", false);
      enableInput("submit-btn", false);
      var processName = el("process_select").value.trim();
      if (!processName) {
        return;
      }
      validateMachineProcess();
    });

    el("start-btn").addEventListener("click", function () {
      if (!state.machineValidated || state.processStarted) {
        return;
      }

      var payload = {
        mode: "START",
        work_order: state.workOrder,
        machine_id: state.machineId,
        process_name: state.processName,
        operator_id: state.operatorId,
      };

      setStatus("Starting process...", "");
      enableInput("start-btn", false);

      postJson(urls.submit, payload)
        .then(function (data) {
          setStatus("Started! Redirecting...", "success");
          // Redirect to WIP home page
          window.location.href = "/wip/";
        })
        .catch(function (err) {
          setStatus(err.message, "error");
          enableInput("start-btn", true);
        });
    });

    function attachNumericAdvance(input, nextId) {
      if (!input) {
        return;
      }
      var idleTimer = null;
      function goNext() {
        if (input.disabled || !input.value.trim()) {
          return;
        }
        focusInput(nextId);
      }
      function schedule() {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(goNext, SCAN_IDLE_MS);
      }
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          goNext();
        }
      });
      input.addEventListener("input", schedule);
      input.addEventListener("keyup", function (event) {
        if (event.key !== "Enter") {
          schedule();
        }
      });
    }

    attachNumericAdvance(el("production_count"), "rejected_count");

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!state.processStarted) {
        setStatus("Press Start first", "error");
        return;
      }
      setStatus("Submitting…", "");
      enableInput("submit-btn", false);

      // Use state values if in-progress, otherwise use form values
      var payload = {
        work_order: state.workOrder,
        operator_id: state.inProgress
          ? state.operatorId
          : el("operator_id").value,
        machine_id: state.inProgress ? state.machineId : el("machine_id").value,
        process_name: state.inProgress
          ? state.processName
          : el("process_select").value,
        material_code: state.materialCode,
        part_name: state.partName,
        production_count: el("production_count").value,
        rejected_count: el("rejected_count").value,
        process_started: true,
      };

      postJson(urls.submit, payload)
        .then(function (data) {
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

    focusInput("work_order");
    setStatus("Scan work order", "");
  });
})();
