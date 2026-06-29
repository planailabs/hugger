/** Tailwind config for hugger. Scans the FastHTML markup (Python) for class
 *  names and builds a self-hosted stylesheet (no CDN). Design tokens mirror the
 *  claude.ai/design system. */
module.exports = {
  content: ["./hugger/**/*.py"],
  theme: {
    extend: {
      colors: {
        cream: "#FDF6E9", surface: "#FFFDF8", surface2: "#FBF1DC",
        line: "#EAB378", linesoft: "#F1E2C6", lineth: "#E7CFA4", divider: "#F2DDBB",
        ink: "#3A2A1A", muted: "#6B5840", muted2: "#8A6D4A", th: "#7A5B36",
        accent: "#EA580C", accenth: "#D9500B", accent2: "#F97316",
        accentink: "#B43E08", accentink2: "#A8370A",
        ghostb: "#E59A4E", ghosth: "#FFF3E0", inputb: "#E0C089",
        danger: "#DC2626", dangerh: "#B91C1C",
        okbg: "#DCFCE7", okfg: "#15803D", warnbg: "#FEF3C7", warnfg: "#92400E",
        errbg: "#FEE2E2", errfg: "#B91C1C", infobg: "#DBEAFE", infofg: "#1E40AF",
        neutralbg: "#EEE4D0", neutralfg: "#5A4632",
        focus: "#0B5FFF",
      },
      fontFamily: {
        sans: ["'Nunito Sans'", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
        brand: ["'Baloo 2'", "'Nunito Sans'", "system-ui", "sans-serif"],
        mono: ["'JetBrains Mono'", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      backgroundImage: {
        // original hugger header gradient (amber -> deep orange)
        "hdr": "linear-gradient(90deg,#FB8C00,#F4511E)",
        "fill": "linear-gradient(90deg,#F97316,#EA580C)",
      },
    },
  },
};
