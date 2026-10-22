// Stiding 排班管理系统 · 联动下拉：队组 -> 班组 -> 班次配置
(function () {
  function fillSelect(select, items, textFn, placeholder, keepValue) {
    var keep = keepValue != null ? keepValue : select.value;
    select.innerHTML = "";
    var ph = document.createElement("option");
    ph.value = "";
    ph.textContent = placeholder;
    select.appendChild(ph);
    items.forEach(function (item) {
      var opt = document.createElement("option");
      opt.value = item.id;
      opt.textContent = textFn(item);
      select.appendChild(opt);
    });
    var hasValue = Array.prototype.some.call(select.options, function (o) {
      return o.value === String(keep);
    });
    if (keep && hasValue) {
      select.value = keep;
    }
  }

  function clearSelect(select, placeholder) {
    fillSelect(select, [], placeholder || "请先选择上级选项");
    select.disabled = true;
  }

  // 队组 -> 班组
  function bindOrgToTeam(orgSel, teamSel, url) {
    if (!orgSel || !teamSel) { return; }
    orgSel.addEventListener("change", function () {
      var orgId = orgSel.value;
      var dependent = document.querySelector(teamSel.dataset.cascadeCfg || "");
      if (dependent) { clearSelect(dependent, "请先选择班组"); }
      if (!orgId) {
        clearSelect(teamSel, "请先选择队组");
        return;
      }
      teamSel.disabled = true;
      fetch(url + "?organization=" + encodeURIComponent(orgId))
        .then(function (res) { return res.json(); })
        .then(function (data) {
          fillSelect(
            teamSel,
            data.teams,
            function (t) { return t.name + "（" + t.code + "）" + (t.is_active ? "" : " · 已停用"); },
            "请选择班组"
          );
          teamSel.disabled = false;
        })
        .catch(function () { teamSel.disabled = false; });
    });
  }

  // 班组 -> 班次配置
  function bindTeamToConfigs(teamSel, cfgSel, url) {
    if (!teamSel || !cfgSel) { return; }
    var load = function () {
      var teamId = teamSel.value;
      if (!teamId) {
        clearSelect(cfgSel, "请先选择班组");
        return;
      }
      cfgSel.disabled = true;
      fetch(url + "?team=" + encodeURIComponent(teamId))
        .then(function (res) { return res.json(); })
        .then(function (data) {
          fillSelect(
            cfgSel,
            data.shift_configs,
            function (c) { return c.display + (c.is_active ? "" : "（待配置）"); },
            "请选择班次"
          );
          cfgSel.disabled = false;
        })
        .catch(function () { cfgSel.disabled = false; });
    };
    teamSel.addEventListener("change", load);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var urls = window.CASCADE_URLS || {};

    // 表单页：班组表单（队组 -> 班组无第二级，无需处理）、职工表单
    var formTeam = document.getElementById("id_team");
    var formCfg = document.getElementById("id_shift_config");
    if (formTeam && formCfg && urls.shiftConfigs) {
      bindTeamToConfigs(formTeam, formCfg, urls.shiftConfigs);
      if (formTeam.value) { formTeam.dispatchEvent(new Event("change")); }
    }

    // 筛选条：职工列表 / 班次配置 / 排班计划 / 排班结果
    var fOrg = document.getElementById("filter-organization");
    var fTeam = document.getElementById("filter-team");
    var fCfg = document.getElementById("filter-shift-config");
    if (fOrg && fTeam && urls.teams) { bindOrgToTeam(fOrg, fTeam, urls.teams); }
    if (fTeam && fCfg && urls.shiftConfigs) { bindTeamToConfigs(fTeam, fCfg, urls.shiftConfigs); }
  });
})();
