"use strict";

// Small, dependency-free SVG instruments. Values remain unrounded internally;
// only the displayed number is formatted. An absent value never becomes zero.
(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const CENTER = { x: 200, y: 190 };
  const RADII = { inner: 114, band: 169, majorOuter: 156, majorInner: 139, minorOuter: 156, minorInner: 148, label: 182, needleTip: 164, needleBase: 86 };
  let nextId = 0;

  function svgElement(name, attributes = {}, text = null) {
    const element = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => {
      element.setAttribute(key, String(value));
    });
    if (text !== null) element.textContent = text;
    return element;
  }

  function point(fraction, radius) {
    const angle = Math.PI * (1 - fraction);
    return {
      x: CENTER.x + Math.cos(angle) * radius,
      y: CENTER.y - Math.sin(angle) * radius,
    };
  }

  function arc(start, end, radius) {
    const from = point(start, radius);
    const to = point(end, radius);
    return `M ${from.x} ${from.y} A ${radius} ${radius} 0 ${end - start > 1 ? 1 : 0} 1 ${to.x} ${to.y}`;
  }

  class DialGauge {
    constructor(container, options = {}) {
      if (typeof container === "string") container = document.getElementById(container);
      if (!container) throw new Error("A gauge container is required.");
      this.container = container;
      this.min = options.min ?? 0;
      this.max = options.max ?? 100;
      if (!Number.isFinite(this.min) || !Number.isFinite(this.max) || this.max <= this.min) {
        throw new RangeError("A gauge needs a finite, increasing scale.");
      }
      this.id = options.id || container.id.replace(/-gauge$/, "") || `dial-${++nextId}`;
      this.label = options.label ?? options.title ?? "Measurement";
      this.unit = options.unit ?? "";
      this.color = options.color ?? "#43d3ad";
      this.decimals = Number.isInteger(options.decimals)
        ? Math.max(0, Math.min(3, options.decimals)) : 1;
      this.bands = (options.bands || []).filter((band) => (
        Number.isFinite(band.from) && Number.isFinite(band.to) && band.to > band.from
      ));
      this.bandPaths = [];

      const ticks = options.ticks || Array.from({ length: 6 }, (_, i) => (
        this.min + (this.max - this.min) * i / 5
      ));
      this.ticks = ticks.filter((tick) => Number.isFinite(tick) && tick >= this.min && tick <= this.max);

      this.instrument = document.createElement("div");
      this.instrument.className = "dial-instrument";
      const heading = document.createElement("h3");
      heading.className = "dial-title";
      heading.id = `${this.id}-title`;
      heading.textContent = this.label;
      this.face = document.createElement("div");
      this.face.className = "dial-face";

      this.svg = svgElement("svg", {
        viewBox: "0 0 400 240",
        class: "dial-svg dial-gauge",
        role: "img",
        "aria-label": `${this.label}: unavailable`,
        focusable: "false",
      });
      this.svg.style.setProperty("--gauge-color", this.color);
      this.svg.append(
        svgElement("path", {
          d: arc(0, 1, RADII.inner), class: "dial-inner-arc", fill: "none",
          stroke: "var(--dial-track, #485762)", "stroke-width": 0.8,
        }),
      );

      this.bands.forEach((band) => {
        const from = Math.max(0, this.fraction(band.from));
        const to = Math.min(1, this.fraction(band.to));
        if (to <= from) return;
        const bandPath = svgElement("path", {
          d: arc(from, to, RADII.band),
          class: "dial-band", fill: "none", stroke: band.color || this.color,
          "stroke-width": 3, opacity: 0.35,
        });
        this.svg.append(bandPath);
        this.bandPaths.push({ band, element: bandPath });
      });

      const majorFractions = this.ticks.map((tick) => this.fraction(tick));
      const minorCount = Number.isInteger(options.minorTicks)
        ? Math.max(0, Math.min(100, options.minorTicks)) : 40;
      for (let i = 0; i <= minorCount && minorCount > 0; i += 1) {
        const fraction = i / minorCount;
        if (majorFractions.some((major) => Math.abs(major - fraction) < 0.002)) continue;
        const outer = point(fraction, RADII.minorOuter);
        const inner = point(fraction, RADII.minorInner);
        this.svg.append(svgElement("line", {
          x1: outer.x, y1: outer.y, x2: inner.x, y2: inner.y,
          class: "dial-tick dial-tick-minor", stroke: "var(--dial-tick, #b5c3cb)",
          "stroke-width": 0.8, opacity: 0.65,
        }));
      }
      this.ticks.forEach((tick) => {
        const fraction = this.fraction(tick);
        const outer = point(fraction, RADII.majorOuter);
        const inner = point(fraction, RADII.majorInner);
        const textPosition = point(fraction, RADII.label);
        const atLeftEndpoint = fraction === 0;
        const atRightEndpoint = fraction === 1;
        // Put endpoint numbers below the axis, away from the band and ticks.
        // Other numbers sit outside the band on the same radial geometry.
        if (atLeftEndpoint || atRightEndpoint) textPosition.y = CENTER.y + 18;
        this.svg.append(
          svgElement("line", {
            x1: outer.x, y1: outer.y, x2: inner.x, y2: inner.y,
            class: "dial-tick dial-tick-major", stroke: "var(--dial-tick, #b5c3cb)",
            "stroke-width": 1.1,
          }),
          svgElement("text", {
            x: textPosition.x, y: textPosition.y,
            class: "dial-tick-label", fill: "var(--dial-muted, #778b98)", stroke: "none",
            "text-anchor": atLeftEndpoint ? "start" : atRightEndpoint ? "end" : "middle",
            "dominant-baseline": "central",
            "font-family": "JetBrains Mono, monospace", "font-size": 13,
            "font-weight": 300,
          }, Number.isInteger(tick) ? String(tick) : String(Number(tick.toFixed(1)))),
        );
      });

      this.needle = svgElement("g", { class: "dial-needle gauge-needle", "aria-hidden": "true" });
      this.needle.setAttribute("transform", `rotate(-90 ${CENTER.x} ${CENTER.y})`);
      const needleTip = point(0.5, RADII.needleTip);
      const needleBase = point(0.5, RADII.needleBase);
      this.needle.append(svgElement("polygon", {
        points: `${needleTip.x},${needleTip.y} ${needleBase.x - 5},${needleBase.y} ${needleBase.x + 5},${needleBase.y}`,
        fill: "var(--gauge-color)", stroke: "none",
      }));
      this.svg.append(this.needle);

      this.readout = document.createElement("div");
      this.readout.className = "dial-readout";
      // The SVG's accessible label includes this same reading and unit.
      this.readout.setAttribute("aria-hidden", "true");
      this.valueText = document.createElement("span");
      this.valueText.id = `${this.id}-value`;
      this.valueText.className = "dial-value";
      this.valueText.textContent = "—";
      const unitText = document.createElement("span");
      unitText.className = "dial-unit";
      unitText.textContent = this.unit;
      this.readout.append(this.valueText, unitText);
      this.face.append(this.svg, this.readout);
      this.instrument.append(heading, this.face);
      container.replaceChildren(this.instrument);
      this.setValue(options.value ?? null);
    }

    fraction(value) {
      return (value - this.min) / (this.max - this.min);
    }

    setValue(value) {
      const available = Number.isFinite(value);
      this.svg.dataset.available = String(available);
      this.instrument.dataset.available = String(available);
      this.needle.style.visibility = available ? "visible" : "hidden";
      if (!available) {
        this.valueText.textContent = "—";
        this.svg.removeAttribute("data-out-of-range");
        this.svg.setAttribute("aria-label", `${this.label}: unavailable`);
        this.bandPaths.forEach(({ element }) => element.setAttribute("opacity", "0.25"));
        return;
      }

      const fraction = Math.max(0, Math.min(1, this.fraction(value)));
      const displayValue = Number(value.toFixed(this.decimals)) === 0 ? 0 : value;
      this.valueText.textContent = displayValue.toFixed(this.decimals);
      this.needle.setAttribute("transform", `rotate(${fraction * 180 - 90} ${CENTER.x} ${CENTER.y})`);
      const outOfRange = value < this.min ? "below" : value > this.max ? "above" : "";
      if (outOfRange) this.svg.dataset.outOfRange = outOfRange;
      else this.svg.removeAttribute("data-out-of-range");
      this.svg.setAttribute("aria-label", `${this.label}: ${this.valueText.textContent} ${this.unit}${outOfRange ? `, ${outOfRange} the displayed scale` : ""}`);
      let currentColor = this.color;
      const bandValue = Math.max(this.min, Math.min(this.max, value));
      this.bandPaths.forEach(({ band, element }) => {
        const active = bandValue >= band.from && (bandValue < band.to || (band.to === this.max && bandValue === this.max));
        element.setAttribute("opacity", active ? "1" : "0.35");
        if (active) currentColor = band.color || currentColor;
      });
      this.svg.style.setProperty("--gauge-color", currentColor);
    }
  }

  window.DialGauge = DialGauge;
})();
