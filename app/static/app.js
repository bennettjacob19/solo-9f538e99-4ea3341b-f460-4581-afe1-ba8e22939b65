/* Dynamic row editing + example presets for the drill form.
 * All submission is a normal HTML form POST; JS only improves editing. */
(function () {
  "use strict";

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function listeningRow(values) {
    values = values || {};
    var tr = document.createElement("tr");
    tr.className = "l-row";
    tr.innerHTML =
      '<td><input name="l_source" placeholder="* 或 10.0.0.1" value="' + esc(values.source) + '"></td>' +
      '<td><input name="l_group" placeholder="239.1.1.1" value="' + esc(values.group) + '"></td>' +
      '<td><input name="l_ports" placeholder="1,2,3" value="' + esc(values.ports) + '"></td>' +
      '<td><button type="button" class="btn tiny danger row-del">删</button></td>';
    return tr;
  }

  function trafficRow(values) {
    values = values || {};
    var tr = document.createElement("tr");
    tr.className = "t-row";
    tr.innerHTML =
      '<td><input name="t_source" placeholder="10.0.0.1" value="' + esc(values.source) + '"></td>' +
      '<td><input name="t_group" placeholder="239.1.1.1" value="' + esc(values.group) + '"></td>' +
      '<td><input name="t_frames" inputmode="numeric" placeholder="1000" value="' + esc(values.frames) + '"></td>' +
      '<td><button type="button" class="btn tiny danger row-del">删</button></td>';
    return tr;
  }

  function addListening(values) {
    document.querySelector("#listening-table tbody").appendChild(listeningRow(values));
  }
  function addTraffic(values) {
    document.querySelector("#traffic-table tbody").appendChild(trafficRow(values));
  }

  document.addEventListener("click", function (ev) {
    if (ev.target.id === "add-listening") addListening();
    if (ev.target.id === "add-traffic") addTraffic();
    if (ev.target.classList.contains("row-del")) {
      var tr = ev.target.closest("tr");
      if (tr) tr.remove();
    }
    if (ev.target.matches("[data-example]")) {
      var key = ev.target.getAttribute("data-example");
      var dataEl = document.getElementById("examples-data");
      if (!dataEl) return;
      var ex = JSON.parse(dataEl.textContent)[key];
      if (!ex) return;

      var lBody = document.querySelector("#listening-table tbody");
      var tBody = document.querySelector("#traffic-table tbody");
      lBody.innerHTML = "";
      tBody.innerHTML = "";

      ex.listening.forEach(function (e) {
        addListening({
          source: e.source || "",
          group: e.group,
          ports: (e.ports || []).join(", ")
        });
      });
      ex.traffic.forEach(function (f) {
        addTraffic({ source: f.source, group: f.group, frames: f.frames });
      });
      // Keep a couple of blank rows for manual entry.
      addListening(); addListening();
      addTraffic(); addTraffic();

      document.querySelector('input[name="hw_limit"]').value = ex.limits.hw;
      document.querySelector('input[name="cpu_limit"]').value = ex.limits.cpu;
      document.querySelector('input[name="simulate_fail"]').checked = !!ex.simulate_fail;

      ev.target.closest(".examples").querySelectorAll(".btn").forEach(function (b) {
        b.classList.remove("primary");
      });
      ev.target.classList.add("primary");
    }
  });
})();
