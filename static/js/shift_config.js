// Stiding 排班管理系统 · 班次配置页交互：岗位人数配置 + 豁免职工弹窗多选
// 加固说明：
//  - 事件委托绑定（document 级），即使目标元素被重新渲染也不会失效；
//  - 各功能块独立 try/catch，失败时 console.error 显式上报，避免静默无反应。
(function () {
  function readJson(id) {
    var el = document.getElementById(id);
    if (!el) return [];
    try { return JSON.parse(el.textContent); } catch (e) { return []; }
  }

  document.addEventListener("DOMContentLoaded", function () {
    // ---------- 岗位人数配置 ----------
    try {
      var form = document.getElementById("config-form");
      var hidden = document.getElementById("id_position_requirements");
      var posError = document.getElementById("pos-error");
      var inputs = document.querySelectorAll("input[data-pos-input]");

      if (form && hidden) {
        form.addEventListener("submit", function (event) {
          var base = {};
          try {
            var parsed = JSON.parse(hidden.value || "{}");
            if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) base = parsed;
          } catch (e) { base = {}; }

          var valid = true;
          var firstBad = null;
          inputs.forEach(function (inp) {
            var raw = inp.value;
            var value = Number(raw);
            if (raw === "" || !Number.isInteger(value) || value < 0) {
              valid = false;
              inp.classList.add("input-invalid");
              if (!firstBad) firstBad = inp;
            } else {
              inp.classList.remove("input-invalid");
              base[inp.dataset.pos] = value;
            }
          });

          if (posError) {
            posError.textContent = valid ? "" : "岗位要求人数只允许非负整数，请修正后重试。";
            posError.classList.toggle("hidden", valid);
          }
          if (!valid) {
            event.preventDefault();
            if (firstBad) firstBad.focus();
            return;
          }
          hidden.value = JSON.stringify(base);
        });
        inputs.forEach(function (inp) {
          inp.addEventListener("input", function () { inp.classList.remove("input-invalid"); });
        });
      }

      document.querySelectorAll("button[data-orphan]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var name = btn.dataset.orphan;
          var tr = btn.closest("tr");
          if (tr) tr.remove();
          if (hidden && hidden.value) {
            try {
              var data = JSON.parse(hidden.value || "{}");
              if (data && typeof data === "object") {
                delete data[name];
                hidden.value = JSON.stringify(data);
              }
            } catch (e) { /* 忽略 */ }
          }
        });
      });
    } catch (e) {
      if (window.console) console.error("岗位人数配置初始化失败：", e);
    }

    // ---------- 豁免职工弹窗多选 ----------
    try {
      var modal = document.getElementById("exempt-modal");
      if (!modal) return;

      var searchInput = document.getElementById("exempt-search");
      var listBox = document.getElementById("exempt-list");
      var countBox = document.getElementById("exempt-count");
      var chipsBox = document.getElementById("exempt-chips");
      var candidates = readJson("exempt-candidates-data");
      var allMembers = readJson("exempt-all-members-data");
      var nameMap = {};
      allMembers.forEach(function (m) { nameMap[m.id] = m.employee_no + " " + m.name; });

      function checkboxOf(id) {
        return document.querySelector('#exempt-checkboxes input[value="' + id + '"]');
      }
      function isChecked(id) {
        var cb = checkboxOf(id);
        return !!cb && cb.checked;
      }
      function setChecked(id, checked) {
        var cb = checkboxOf(id);
        if (cb) cb.checked = checked;
      }
      function checkedIds() {
        var ids = [];
        document.querySelectorAll("#exempt-checkboxes input:checked").forEach(function (cb) {
          ids.push(parseInt(cb.value, 10));
        });
        return ids;
      }
      function filteredCandidates() {
        var kw = (searchInput ? searchInput.value : "").trim().toLowerCase();
        if (!kw) return candidates;
        return candidates.filter(function (c) {
          return (c.name || "").toLowerCase().indexOf(kw) > -1 ||
            (c.employee_no || "").toLowerCase().indexOf(kw) > -1 ||
            (c.positions || []).join("、").toLowerCase().indexOf(kw) > -1;
        });
      }
      function updateCount() {
        if (countBox) countBox.textContent = "已选 " + checkedIds().length + " 人";
      }
      function renderList() {
        var items = filteredCandidates();
        listBox.innerHTML = "";
        items.forEach(function (c) {
          var label = document.createElement("label");
          label.className = "exempt-item";
          var cb = document.createElement("input");
          cb.type = "checkbox";
          cb.value = c.id;
          cb.checked = isChecked(c.id);
          cb.addEventListener("change", function () {
            setChecked(c.id, cb.checked);
            updateCount();
          });
          var info = document.createElement("span");
          info.className = "exempt-item-info";
          info.textContent = c.name + "（" + c.employee_no + "）";
          var pos = document.createElement("span");
          pos.className = "exempt-item-pos";
          pos.textContent = (c.positions && c.positions.length) ? c.positions.join("、") : "未设置岗位";
          label.appendChild(cb);
          label.appendChild(info);
          label.appendChild(pos);
          listBox.appendChild(label);
        });
        if (!items.length) {
          var empty = document.createElement("div");
          empty.className = "empty-state";
          var h = document.createElement("h3");
          h.textContent = "无匹配人员";
          var p = document.createElement("p");
          p.textContent = "当前班次暂无符合筛选条件的在职成员。";
          empty.appendChild(h);
          empty.appendChild(p);
          listBox.appendChild(empty);
        }
        updateCount();
      }
      function renderChips() {
        var ids = checkedIds();
        chipsBox.innerHTML = "";
        if (!ids.length) {
          var empty = document.createElement("span");
          empty.className = "text-muted";
          empty.textContent = "暂无豁免职工";
          chipsBox.appendChild(empty);
          return;
        }
        ids.forEach(function (id) {
          var chip = document.createElement("span");
          chip.className = "chip";
          chip.textContent = nameMap[id] || ("职工#" + id);
          var remove = document.createElement("button");
          remove.type = "button";
          remove.className = "chip-remove";
          remove.setAttribute("aria-label", "移除");
          remove.textContent = "×";
          remove.dataset.chipRemove = String(id);
          remove.addEventListener("click", function () {
            setChecked(id, false);
            renderChips();
            updateCount();
          });
          chip.appendChild(remove);
          chipsBox.appendChild(chip);
        });
      }

      var snapshot = [];
      function takeSnapshot() {
        snapshot = [];
        document.querySelectorAll("#exempt-checkboxes input").forEach(function (cb) {
          snapshot.push({ id: parseInt(cb.value, 10), checked: cb.checked });
        });
      }
      function restoreSnapshot() {
        snapshot.forEach(function (pair) { setChecked(pair.id, pair.checked); });
      }
      function openModal() {
        takeSnapshot();
        if (searchInput) searchInput.value = "";
        renderList();
        modal.classList.add("open");
      }
      function closeWithoutApply() {
        modal.classList.remove("open");
        restoreSnapshot();
        renderChips();
        updateCount();
      }
      function confirmApply() {
        modal.classList.remove("open");
        renderChips();
        updateCount();
      }
      function selectAllFiltered() {
        filteredCandidates().forEach(function (c) { setChecked(c.id, true); });
        renderList();
      }
      function clearAllFiltered() {
        filteredCandidates().forEach(function (c) { setChecked(c.id, false); });
        renderList();
      }

      // 事件委托绑定：点击冒泡到 document 统一处理，元素被重新渲染也不会失效
      document.addEventListener("click", function (event) {
        var target = event.target;
        while (target && target !== document) {
          if (target.id === "exempt-open") { openModal(); return; }
          if (target.id === "exempt-select-all") { selectAllFiltered(); return; }
          if (target.id === "exempt-clear-all") { clearAllFiltered(); return; }
          if (target.id === "exempt-confirm") { confirmApply(); return; }
          if (target.id === "exempt-cancel") { closeWithoutApply(); return; }
          if (target.classList && target.classList.contains("chip-remove")) {
            var removeId = parseInt(target.dataset.chipRemove || target.getAttribute("data-chip-remove"), 10);
            if (!isNaN(removeId)) {
              setChecked(removeId, false);
              renderChips();
              updateCount();
            }
            return;
          }
          target = target.parentElement;
        }
      });
      if (searchInput) searchInput.addEventListener("input", renderList);
      modal.addEventListener("click", function (event) {
        if (event.target === modal) closeWithoutApply();
      });

      renderChips();
      updateCount();
    } catch (e) {
      if (window.console) console.error("豁免职工弹窗初始化失败：", e);
    }
  });
})();
