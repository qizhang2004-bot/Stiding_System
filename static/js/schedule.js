// Stiding 排班管理系统 · 排班结果页：布局示例预览（纯前端示例数据，不写入数据库）
(function () {
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("preview-toggle");
    var box = document.getElementById("schedule-preview");
    if (!btn || !box) { return; }
    var built = false;
    btn.addEventListener("click", function () {
      var hidden = box.classList.toggle("hidden");
      if (!hidden && !built) {
        buildPreview(box);
        built = true;
      }
      btn.textContent = hidden ? "查看布局示例" : "隐藏布局示例";
    });
  });

  function buildPreview(box) {
    var datesEl = document.getElementById("preview-dates");
    if (!datesEl) { return; }
    var dates = JSON.parse(datesEl.textContent);
    var names = ["张三", "李四", "王五", "赵六", "钱七", "孙八"];
    var shifts = ["早班", "中班", "夜班"];
    var shiftClass = { "早班": "cell-early", "中班": "cell-middle", "夜班": "cell-night" };

    var table = document.createElement("table");
    table.className = "schedule-grid";

    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    ["职工", "岗位"].forEach(function (label) {
      var th = document.createElement("th");
      th.className = "sticky-col";
      th.textContent = label;
      headRow.appendChild(th);
    });
    dates.forEach(function (d) {
      var th = document.createElement("th");
      th.className = "date-head";
      th.innerHTML = d.day + '<span class="weekday">' + d.weekday + "</span>";
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    names.forEach(function (name, i) {
      var tr = document.createElement("tr");
      var tdName = document.createElement("td");
      tdName.className = "sticky-col";
      tdName.textContent = name;
      tr.appendChild(tdName);
      var tdPos = document.createElement("td");
      tdPos.className = "sticky-col cell-muted";
      tdPos.textContent = "值班员";
      tr.appendChild(tdPos);
      dates.forEach(function (d, j) {
        var td = document.createElement("td");
        var isRest = (i + j) % 4 === 3;
        if (isRest) {
          td.textContent = "休";
          td.className = "cell-rest";
        } else {
          var shift = shifts[(i + j) % 3];
          td.textContent = shift;
          td.className = shiftClass[shift];
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(table);
  }
})();
