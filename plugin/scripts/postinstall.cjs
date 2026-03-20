#!/usr/bin/env node
/**
 * postinstall.js — verify installation and report status
 */

const { execFileSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const PKG_ROOT = path.resolve(__dirname, '..');

function check(label, fn) {
  try {
    const result = fn();
    console.log(`  ✓ ${label}` + (result ? ` — ${result}` : ''));
    return true;
  } catch (e) {
    console.log(`  ✗ ${label} — ${e.message}`);
    return false;
  }
}

console.log('\neacn postinstall\n');

let ok = true;

ok = check('plugin manifest', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(PKG_ROOT, 'openclaw.plugin.json'), 'utf8'));
  return `id=${manifest.id} v${manifest.version}`;
}) && ok;

ok = check('dist/index.js', () => {
  const p = path.join(PKG_ROOT, 'dist', 'index.js');
  if (!fs.existsSync(p)) throw new Error('not found — run "npm run build" first');
  return 'exists';
}) && ok;

ok = check('dist/server.js', () => {
  const p = path.join(PKG_ROOT, 'dist', 'server.js');
  if (!fs.existsSync(p)) throw new Error('not found — run "npm run build" first');
  return 'exists';
}) && ok;

ok = check('skill files', () => {
  const skillsDir = path.join(PKG_ROOT, 'skills');
  if (!fs.existsSync(skillsDir)) throw new Error('skills/ not found');
  const skills = fs.readdirSync(skillsDir).filter(d =>
    fs.statSync(path.join(skillsDir, d)).isDirectory()
  );
  return `${skills.length} skills (${skills.join(', ')})`;
}) && ok;

if (ok) {
  console.log('\n  All checks passed.\n');
} else {
  console.log('\n  Some checks failed — the plugin may not work correctly.');
  console.log('  Run "npx eacn diagnose" for details.\n');
}

console.log('Next steps:');
console.log('  npx eacn setup    — install into OpenClaw');
console.log('  npx eacn diagnose — run full diagnostics\n');
