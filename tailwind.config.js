const { tailwindPreset } = require("@atmosphere/tailwind");

/** @type {import('tailwindcss').Config} */
module.exports = {
  presets: [tailwindPreset],
  corePlugins: {
    preflight: false,
  },
  darkMode: ["class", ".hbr-mode-dark"],
  content: ["./static/index.html", "./static/src/**/*.css"],
};
