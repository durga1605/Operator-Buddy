"use strict";

/**
 * WIP start/complete — barcode scan workflow.
 * Auto-advances on scanner input (fast burst + idle) or Enter suffix.
 */

(function () {
  var SCAN_IDLE_MS = 120;
  var SCAN_MAX_DURATION_MS = 600;
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

  function setStatus(message, type) {
    var el = document.getElementById("wip-scan-status");
    if (!el) {
      return;
    }
    el.textContent = message || "";
    el.classList.remove("is-success", "is-error");
    if (type === "success") {
      el.classList.add("is-success");
    } else if (type === "error") {
      el.classList.add("is-error");
    }
  }

  function postJson(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCookie("csrftoken") || "",
      },
      body: JSON.stringify(payload),
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) {
          var err = new Error(data.error || "Request failed");
          err.payload = data;
          throw err;
        }
        return data;
      });
    });
  }

  /**
   * Bind barcode-style auto-submit: Enter and rapid scan idle detection.
   */
  function attachBarcodeScanner(input, onScan) {
    var idleTimer = null;
    var firstKeyTime = 0;
    var busy = false;

    function clearIdleTimer() {
      if (idleTimer) {
        clearTimeout(idleTimer);
        idleTimer = null;
      }
    }

    function commit() {
      clearIdleTimer();
      if (busy || input.disabled) {
        return;
      }
      var value = input.value.trim();
      if (value.length < MIN_SCAN_LENGTH) {
        return;
      }
      busy = true;
      Promise.resolve(onScan(value))
        .catch(function () {
          /* errors handled in onScan */
        })
        .finally(function () {
          busy = false;
          firstKeyTime = 0;
        });
    }

    function scheduleIdleCheck() {
      clearIdleTimer();
      idleTimer = setTimeout(function () {
        var duration = Date.now() - firstKeyTime;
        var len = input.value.trim().length;
        if (len >= MIN_SCAN_LENGTH && duration <= SCAN_MAX_DURATION_MS) {
          commit();
        }
        firstKeyTime = 0;
      }, SCAN_IDLE_MS);
    }

    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        commit();
        return;
      }
      if (!firstKeyTime) {
        firstKeyTime = Date.now();
      }
    });

    input.addEventListener("input", function () {
      if (!firstKeyTime) {
        firstKeyTime = Date.now();
      }
      scheduleIdleCheck();
    });

    input.addEventListener("focus", function () {
      input.classList.add("wip-scan-input--active");
    });

    input.addEventListener("blur", function () {
      input.classList.remove("wip-scan-input--active");
    });
  }

  onReady(function () {
    var form = document.getElementById("wip-form");
    if (!form) {
      return;
    }

    var urls = {
      part: form.getAttribute("data-url-part"),
      operator: form.getAttribute("data-url-operator"),
      machine: form.getAttribute("data-url-machine"),
      workOrder: form.getAttribute("data-url-work-order"),
      submit: form.getAttribute("data-url-submit"),
    };

    var state = {
      partScanned: false,
      operatorScanned: false,
      machineScanned: false,
      woScanned: false,
      materialCode: "",
      partName: "",
      sapPlantCode: "",
      timerInterval: null,
      seconds: 0,
    };

    function el(id) {
      return document.getElementById(id);
    }

    function enableField(id, enable) {
      var field = el(id);
      if (field) {
        field.disabled = !enable;
      }
    }

    function focusField(id) {
      var field = el(id);
      if (field && !field.disabled) {
        field.focus();
        field.select();
      }
    }

    function lockField(id) {
      var field = el(id);
      if (field) {
        field.disabled = true;
        field.classList.remove("wip-scan-input--active");
      }
    }

    function startTimer() {
      if (state.timerInterval) {
        return;
      }
      state.timerInterval = setInterval(function () {
        state.seconds += 1;
        var h = String(Math.floor(state.seconds / 3600)).padStart(2, "0");
        var m = String(Math.floor((state.seconds % 3600) / 60)).padStart(
          2,
          "0",
        );
        var s = String(state.seconds % 60).padStart(2, "0");
        var timerEl = el("timer");
        if (timerEl) {
          timerEl.textContent = "Timer: " + h + ":" + m + ":" + s;
        }
      }, 1000);
    }

    function scanPartNo() {
      if (state.partScanned) {
        return Promise.resolve();
      }
      var partNo = el("part_no").value.trim();
      if (!partNo) {
        return Promise.resolve();
      }
      setStatus("Validating part…", "");
      return postJson(urls.part, { part_no: partNo })
        .then(function (data) {
          if (data.status !== "success") {
            throw new Error(data.error || "Part scan failed");
          }
          state.partScanned = true;
          state.partName = data.part_name;
          state.materialCode = data.material_code;
          state.sapPlantCode = data.sap_plant_code;
          lockField("part_no");
          enableField("operator_id", true);
          setStatus("Part OK — scan operator", "success");
          focusField("operator_id");
        })
        .catch(function (err) {
          setStatus(err.message || "Part not found", "error");
          focusField("part_no");
          throw err;
        });
    }

    function scanOperator() {
      if (state.operatorScanned) {
        return Promise.resolve();
      }
      var opId = el("operator_id").value.trim();
      if (!opId) {
        return Promise.resolve();
      }
      setStatus("Validating operator…", "");
      return postJson(urls.operator, { operator_id: opId })
        .then(function (data) {
          if (data.status !== "success") {
            throw new Error(data.error || "Operator scan failed");
          }
          state.operatorScanned = true;
          lockField("operator_id");
          enableField("machine_id", true);
          setStatus("Operator OK — scan machine", "success");
          focusField("machine_id");
        })
        .catch(function (err) {
          setStatus(err.message || "Operator invalid", "error");
          focusField("operator_id");
          throw err;
        });
    }

    function scanMachine() {
      if (state.machineScanned) {
        return Promise.resolve();
      }
      var machineId = el("machine_id").value.trim();
      if (!machineId) {
        return Promise.resolve();
      }
      setStatus("Validating machine…", "");
      return postJson(urls.machine, {
        machine_id: machineId,
        part_name: state.partName,
      })
        .then(function (data) {
          if (data.status !== "success") {
            throw new Error(data.error || "Machine scan failed");
          }
          state.machineScanned = true;
          lockField("machine_id");

          var procSelect = el("process_select");
          procSelect.innerHTML = '<option value="">Select Process</option>';
          (data.process_list || []).forEach(function (processName) {
            var opt = document.createElement("option");
            opt.value = processName;
            opt.textContent = processName;
            procSelect.appendChild(opt);
          });

          enableField("process_select", true);
          enableField("work_order", true);

          if (data.process_list && data.process_list.length === 1) {
            procSelect.value = data.process_list[0];
          }

          setStatus("Machine OK — scan work order", "success");
          focusField("work_order");
        })
        .catch(function (err) {
          setStatus(err.message || "Machine invalid", "error");
          focusField("machine_id");
          throw err;
        });
    }

    function scanWorkOrder() {
      if (state.woScanned) {
        return Promise.resolve();
      }
      var wo = el("work_order").value.trim();
      if (!wo) {
        return Promise.resolve();
      }
      setStatus("Validating work order…", "");
      return postJson(urls.workOrder, {
        work_order: wo,
        material_code: state.materialCode,
        part_name: state.partName,
      })
        .then(function (data) {
          if (data.status !== "success") {
            throw new Error(data.error || "Work order scan failed");
          }
          state.woScanned = true;
          lockField("work_order");
          var woDisplay = String(data.woqty);
          if (data.remaining_qty !== undefined) {
            woDisplay += " (remaining: " + data.remaining_qty + ")";
          }
          el("woqty").value = woDisplay;
          enableField("production_count", true);
          enableField("rejected_count", true);
          enableField("submit-btn", true);
          startTimer();
          setStatus("Work order OK — enter counts", "success");
          focusField("production_count");
        })
        .catch(function (err) {
          state.woScanned = false;
          enableField("work_order", true);
          var woField = el("work_order");
          if (woField) {
            woField.disabled = false;
            woField.value = "";
          }
          setStatus(err.message || "Work order invalid", "error");
          focusField("work_order");
          throw err;
        });
    }

    attachBarcodeScanner(el("part_no"), scanPartNo);
    attachBarcodeScanner(el("operator_id"), scanOperator);
    attachBarcodeScanner(el("machine_id"), scanMachine);
    attachBarcodeScanner(el("work_order"), scanWorkOrder);

    el("process_select").addEventListener("change", function () {
      if (state.machineScanned && !state.woScanned) {
        focusField("work_order");
      }
    });

    el("production_count").addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        focusField("rejected_count");
      }
    });

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!state.woScanned) {
        return;
      }
      setStatus("Submitting…", "");
      enableField("submit-btn", false);

      postJson(urls.submit, {
        operator_id: el("operator_id").value,
        machine_id: el("machine_id").value,
        work_order: el("work_order").value,
        material_code: state.materialCode,
        part_name: state.partName,
        production_count: el("production_count").value,
        rejected_count: el("rejected_count").value,
      })
        .then(function (data) {
          window.alert(data.message || data.error || "Done");
          if (data.status === "success") {
            window.location.reload();
          }
        })
        .catch(function (err) {
          setStatus(err.message || "Submit failed", "error");
          enableField("submit-btn", true);
        });
    });

    focusField("part_no");
    setStatus("Ready — scan part number", "");
  });
})();
