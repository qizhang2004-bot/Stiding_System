// Stiding 排班管理系统 · 全局交互：侧边栏 / 确认弹窗 / 提示 / 防重复提交 / 文件选择
(function () {
  document.addEventListener("DOMContentLoaded", function () {
    // 侧边栏折叠（记忆状态）
    var toggle = document.getElementById("sidebar-toggle");
    if (toggle) {
      toggle.addEventListener("click", function () {
        var layout = document.querySelector(".layout");
        var collapsed = layout.classList.toggle("sidebar-collapsed");
        try {
          localStorage.setItem("stiding-sidebar", collapsed ? "1" : "0");
        } catch (e) { /* 忽略存储异常 */ }
      });
      try {
        if (localStorage.getItem("stiding-sidebar") === "1") {
          document.querySelector(".layout").classList.add("sidebar-collapsed");
        }
      } catch (e) { /* 忽略 */ }
    }

    // 确认弹窗：带 data-confirm 的表单提交前弹窗确认
    var modal = document.getElementById("confirm-modal");
    if (modal) {
      var modalText = document.getElementById("confirm-text");
      var pendingForm = null;
      document.querySelectorAll("form[data-confirm]").forEach(function (form) {
        form.addEventListener("submit", function (event) {
          if (form.dataset.confirmed === "1") { return; }
          event.preventDefault();
          pendingForm = form;
          modalText.textContent = form.dataset.confirm || "确认执行该操作？";
          modal.classList.add("open");
        });
      });
      document.getElementById("confirm-ok").addEventListener("click", function () {
        modal.classList.remove("open");
        if (pendingForm) {
          pendingForm.dataset.confirmed = "1";
          pendingForm.submit();
        }
      });
      var closeModal = function () {
        modal.classList.remove("open");
        pendingForm = null;
      };
      document.getElementById("confirm-cancel").addEventListener("click", closeModal);
      modal.addEventListener("click", function (event) {
        if (event.target === modal) { closeModal(); }
      });
    }

    // 表单防重复提交
    document.querySelectorAll("form[data-guard-submit]").forEach(function (form) {
      form.addEventListener("submit", function () {
        var btn = form.querySelector("button[type=submit]");
        if (btn && !btn.disabled) {
          btn.disabled = true;
          btn.dataset.original = btn.textContent;
          btn.textContent = "提交中…";
          setTimeout(function () {
            btn.disabled = false;
            if (btn.dataset.original) { btn.textContent = btn.dataset.original; }
          }, 4000);
        }
      });
    });

    // 文件选择：显示文件名与大小
    document.querySelectorAll("input[type=file][data-file-label]").forEach(function (input) {
      var update = function () {
        var target = document.getElementById(input.dataset.fileLabel);
        if (!target) { return; }
        if (input.files && input.files.length) {
          var file = input.files[0];
          var size = file.size > 1048576
            ? (file.size / 1048576).toFixed(1) + " MB"
            : Math.max(1, Math.round(file.size / 1024)) + " KB";
          target.textContent = file.name + "（" + size + "）";
        } else {
          target.textContent = "未选择文件";
        }
      };
      input.addEventListener("change", update);
      update();
    });

    // 页面提示自动消失
    document.querySelectorAll(".toast").forEach(function (toast) {
      setTimeout(function () {
        toast.classList.add("closing");
        setTimeout(function () { toast.remove(); }, 350);
      }, 5000);
      var closeBtn = toast.querySelector(".toast-close");
      if (closeBtn) {
        closeBtn.addEventListener("click", function () { toast.remove(); });
      }
    });
  });
})();
