#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const vendor = path.join(root, "static", "vendor");

function copyDir(from, to) {
  fs.rmSync(to, { recursive: true, force: true });
  fs.mkdirSync(path.dirname(to), { recursive: true });
  fs.cpSync(from, to, { recursive: true });
}

const harborFrom = path.join(root, "node_modules", "@harbor", "elements", "dist", "harbor-elements");
const harborTo = path.join(vendor, "harbor-elements");
if (!fs.existsSync(harborFrom)) {
  console.warn("hoist-atmosphere: @harbor/elements dist not found; skip Harbor copy.");
} else {
  copyDir(harborFrom, harborTo);
  console.log("hoist-atmosphere: copied Harbor assets to static/vendor/harbor-elements");
}

const fontFrom = path.join(root, "node_modules", "@atmosphere", "foundations", "fonts");
const fontTo = path.join(vendor, "atmosphere-fonts");
if (fs.existsSync(fontFrom)) {
  copyDir(fontFrom, fontTo);
  console.log("hoist-atmosphere: copied Atmosphere fonts");
}

const themeCandidates = [
  path.join(root, "node_modules", "@atmosphere", "theme", "themes", "atmosphere.base-16.css"),
  path.join(root, "node_modules", "@atmosphere", "theme", "dist", "themes", "atmosphere.base-16.css"),
  path.join(root, "node_modules", "@atmosphere", "theme", "atmosphere.base-16.css"),
];
const themeToDir = path.join(vendor, "atmosphere-theme");
fs.mkdirSync(themeToDir, { recursive: true });
let themeCopied = false;
for (const themeFrom of themeCandidates) {
  if (fs.existsSync(themeFrom)) {
    fs.copyFileSync(themeFrom, path.join(themeToDir, "atmosphere.base-16.css"));
    themeCopied = true;
    console.log("hoist-atmosphere: copied", path.relative(root, themeFrom));
    break;
  }
}
if (!themeCopied) {
  console.warn("hoist-atmosphere: atmosphere.base-16.css not found; list theme package after install.");
}
