// 首页“排班人数趋势”折线图：零依赖轻量 SVG 渲染。
// 数据由后端按权限聚合后通过 json_script 注入（#trend-data），前端只负责绘制与悬停提示。
// 交互约定：所有折线始终显示（图例不可点击隐藏），鼠标悬停显示日期/队组/班次/人数提示。
(function () {
  "use strict";

  const PALETTE = [
    "#2563eb", "#f59e0b", "#7c3aed", "#059669", "#dc2626", "#0891b2",
    "#db2777", "#65a30d", "#9333ea", "#ea580c", "#0d9488", "#4f46e5",
  ];

  function el(tag, attrs) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  }

  function renderChart(container, payload, tooltip) {
    container.innerHTML = "";
    const labels = payload.labels || [];
    const series = payload.series || [];
    if (!labels.length || !series.length) {
      container.innerHTML = '<p class="text-muted" style="font-size:13px;">当前周期暂无排班数据。</p>';
      return;
    }

    const width = 960;
    const height = 340;
    const padding = { top: 16, right: 18, bottom: 38, left: 48 };
    const plotW = width - padding.left - padding.right;
    const plotH = height - padding.top - padding.bottom;

    const maxValue = Math.max(1, ...series.flatMap((item) => item.data || [0]));
    const yMax = maxValue <= 5 ? maxValue + 1 : Math.ceil(maxValue * 1.1);

    const svg = el("svg", {
      viewBox: `0 0 ${width} ${height}`,
      role: "img",
      "aria-label": "当前排班周期每日各班次排班人数折线图",
      class: "trend-chart-svg",
    });

    // 横向网格与 Y 轴刻度
    const ticks = 5;
    for (let i = 0; i <= ticks; i += 1) {
      const value = Math.round((yMax / ticks) * i);
      const y = padding.top + plotH - (plotH * i) / ticks;
      svg.appendChild(el("line", {
        x1: padding.left, y1: y, x2: padding.left + plotW, y2: y,
        stroke: "#e5e7eb", "stroke-width": 1,
      }));
      const text = el("text", {
        x: padding.left - 8, y: y + 4, "text-anchor": "end",
        "font-size": 11, fill: "#6b7280",
      });
      text.textContent = String(value);
      svg.appendChild(text);
    }

    const stepX = labels.length > 1 ? plotW / (labels.length - 1) : 0;
    const xAt = (index) => padding.left + stepX * index;
    const yAt = (value) => padding.top + plotH - (plotH * value) / yMax;

    // X 轴日期标签（点多时隔项显示，避免重叠）
    const labelEvery = Math.max(1, Math.ceil(labels.length / 12));
    labels.forEach((label, index) => {
      if (index % labelEvery !== 0 && index !== labels.length - 1) return;
      const text = el("text", {
        x: xAt(index), y: padding.top + plotH + 20, "text-anchor": "middle",
        "font-size": 11, fill: "#6b7280",
      });
      text.textContent = label;
      svg.appendChild(text);
    });

    // 折线（始终显示，无点击隐藏）
    series.forEach((item, seriesIndex) => {
      const color = PALETTE[seriesIndex % PALETTE.length];
      const data = item.data || [];
      if (!data.length) return;
      const points = data.map((value, index) => `${xAt(index)},${yAt(value)}`).join(" ");
      svg.appendChild(el("polyline", {
        points,
        fill: "none",
        stroke: color,
        "stroke-width": item.shift === "total" ? 2.4 : 1.8,
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
        opacity: item.shift === "total" ? 1 : 0.9,
      }));
      data.forEach((value, index) => {
        svg.appendChild(el("circle", {
          cx: xAt(index), cy: yAt(value), r: 1.8, fill: color, opacity: 0.9,
        }));
      });
    });

    // 悬停辅助层：覆盖绘图区，按 x 轴最近日期显示提示（鼠标离开即隐藏）
    const hoverLine = el("line", {
      x1: 0, y1: padding.top, x2: 0, y2: padding.top + plotH,
      stroke: "#9ca3af", "stroke-width": 1, "stroke-dasharray": "4 3", opacity: 0,
    });
    svg.appendChild(hoverLine);
    const hoverLayer = el("rect", {
      x: padding.left, y: padding.top, width: plotW, height: plotH,
      fill: "transparent", style: "cursor: crosshair",
    });
    svg.appendChild(hoverLayer);

    container.appendChild(svg);

    // 图例：仅用于识别折线（不可点击隐藏），系列多时区域可滚动
    const legend = document.createElement("div");
    legend.className = "trend-legend";
    series.forEach((item, seriesIndex) => {
      const entry = document.createElement("span");
      entry.className = "trend-legend-item";
      const swatch = document.createElement("span");
      swatch.className = "trend-legend-swatch";
      swatch.style.background = PALETTE[seriesIndex % PALETTE.length];
      const text = document.createElement("span");
      text.textContent = item.name;
      entry.appendChild(swatch);
      entry.appendChild(text);
      legend.appendChild(entry);
    });
    container.appendChild(legend);

    const hideTooltip = () => {
      tooltip.hidden = true;
      hoverLine.setAttribute("opacity", "0");
    };

    const showTooltip = (event) => {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      const svgX = ((event.clientX - rect.left) / rect.width) * width;
      const index = Math.min(
        labels.length - 1,
        Math.max(0, Math.round((svgX - padding.left) / (stepX || 1)))
      );
      hoverLine.setAttribute("x1", xAt(index));
      hoverLine.setAttribute("x2", xAt(index));
      hoverLine.setAttribute("opacity", "1");

      const rows = series.map((item) => ({
        name: item.name,
        org: item.org,
        shift: item.shift,
        value: (item.data || [])[index] ?? 0,
      }));
      const title = document.createElement("div");
      title.className = "trend-tooltip-title";
      title.textContent = `日期 ${labels[index]}`;
      tooltip.innerHTML = "";
      tooltip.appendChild(title);
      rows.forEach((row) => {
        const line = document.createElement("div");
        line.className = "trend-tooltip-row";
        // 超级管理员需要“队组 + 班次”；普通管理员显示班次（含当日总人数）
        const label = row.org && payload.is_super ? `${row.org} · ${shiftLabel(row.shift)}` : shiftLabel(row.shift);
        line.textContent = `${label}：${row.value} 人`;
        tooltip.appendChild(line);
      });
      tooltip.hidden = false;

      const box = container.getBoundingClientRect();
      const left = Math.min(
        Math.max(event.clientX - box.left + 14, 8),
        Math.max(box.width - tooltip.offsetWidth - 8, 8)
      );
      const top = Math.max(event.clientY - box.top + 14, 8);
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${top}px`;
    };

    hoverLayer.addEventListener("pointermove", showTooltip);
    svg.addEventListener("pointermove", showTooltip);
    svg.addEventListener("pointerleave", hideTooltip);
    hoverLayer.addEventListener("pointerleave", hideTooltip);
  }

  function shiftLabel(shift) {
    if (shift === "early") return "早班";
    if (shift === "middle") return "中班";
    if (shift === "night") return "夜班";
    if (shift === "total") return "当日总人数";
    return shift || "";
  }

  document.addEventListener("DOMContentLoaded", () => {
    const container = document.getElementById("trend-chart");
    const dataNode = document.getElementById("trend-data");
    const tooltip = document.getElementById("trend-tooltip");
    if (!container || !dataNode || !tooltip) return;
    let payload;
    try {
      payload = JSON.parse(dataNode.textContent);
    } catch (error) {
      container.innerHTML = '<p class="text-muted" style="font-size:13px;">图表数据解析失败。</p>';
      return;
    }
    renderChart(container, payload, tooltip);
  });
})();
