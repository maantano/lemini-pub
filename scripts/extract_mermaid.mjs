#!/usr/bin/env node
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

const [, , inputPath, outDir] = process.argv;
if (!inputPath || !outDir) {
  console.error("usage: node extract_mermaid.mjs <markdown> <out-dir>");
  process.exit(1);
}

const md = readFileSync(inputPath, "utf8");
mkdirSync(outDir, { recursive: true });

const lines = md.split("\n");
const blocks = [];
let cur = null;
let lastHeading = "";
for (const line of lines) {
  const h = line.match(/^##\s+(.+)/);
  if (h) lastHeading = h[1].trim();
  if (line.startsWith("```mermaid")) {
    cur = { heading: lastHeading, body: [] };
    continue;
  }
  if (cur && line.startsWith("```")) {
    blocks.push(cur);
    cur = null;
    continue;
  }
  if (cur) cur.body.push(line);
}

const slug = (s) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9가-힣\s-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 60);

const manifest = [];
blocks.forEach((b, i) => {
  const idx = String(i + 1).padStart(2, "0");
  const name = `${idx}-${slug(b.heading) || "diagram"}.mmd`;
  const p = resolve(outDir, name);
  writeFileSync(p, b.body.join("\n").trim() + "\n", "utf8");
  manifest.push({ name, heading: b.heading });
  console.log(`wrote ${p}`);
});

writeFileSync(
  resolve(outDir, "manifest.json"),
  JSON.stringify(manifest, null, 2),
  "utf8",
);
console.log(`extracted ${blocks.length} mermaid blocks`);
