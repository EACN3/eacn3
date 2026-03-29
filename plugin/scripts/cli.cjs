#!/usr/bin/env node
/**
 * cli.js — `eacn3` CLI entry point
 * Usage:
 *   npx eacn3 setup     — configure OpenClaw native plugin
 *   npx eacn3 diagnose  — run diagnostics
 */

const { spawnSync, execFileSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

const PKG_ROOT = path.resolve(__dirname, '..');
const PLUGIN_ID = 'eacn3';
const IS_WIN = process.platform === 'win32';
const NPX_CMD = IS_WIN ? 'npx.cmd' : 'npx';
const EXT_DIR = path.join(os.homedir(), '.openclaw', 'extensions', PLUGIN_ID);
const CONFIG_PATH = path.join(os.homedir(), '.openclaw', 'openclaw.json');

// ── helpers ───────────────────────────────────────────────────────────────────

function readJSON(filePath) {
  try { return JSON.parse(fs.readFileSync(filePath, 'utf8')); }
  catch (_) { return {}; }
}

function writeJSON(filePath, obj) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, JSON.stringify(obj, null, 2) + '\n', 'utf8');
}

function merge(target, source) {
  for (const [k, v] of Object.entries(source)) {
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      target[k] = merge(target[k] || {}, v);
    } else {
      target[k] = v;
    }
  }
  return target;
}

function copyDirRecursive(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const entry of fs.readdirSync(src)) {
    const srcPath = path.join(src, entry);
    const dstPath = path.join(dst, entry);
    if (fs.statSync(srcPath).isDirectory()) {
      if (entry === 'node_modules' || entry === '.git') continue;
      copyDirRecursive(srcPath, dstPath);
    } else {
      fs.copyFileSync(srcPath, dstPath);
    }
  }
}

function log(msg) { console.log(`  ${msg}`); }
function ok(msg)  { console.log(`  ✓ ${msg}`); }
function fail(msg){ console.log(`  ✗ ${msg}`); }

function readTOML(filePath) {
  try {
    const text = fs.readFileSync(filePath, 'utf8');
    return { _raw: text, _path: filePath };
  } catch (_) {
    return { _raw: '', _path: filePath };
  }
}

/** Ensure dist/server.js exists, build if needed. Returns absolute path. */
function ensureBuild() {
  const serverJs = path.join(PKG_ROOT, 'dist', 'server.js');
  if (!fs.existsSync(serverJs)) {
    log('dist/server.js not found — building...');
    const build = spawnSync('npm', ['run', 'build'], { cwd: PKG_ROOT, stdio: 'inherit' });
    if (build.status !== 0) {
      fail('build failed');
      process.exit(1);
    }
    ok('build succeeded');
  }
  return serverJs;
}

/** Get absolute path to AGENT_GUIDE.md */
function agentGuidePath() {
  return path.join(PKG_ROOT, 'AGENT_GUIDE.md');
}

// ── diagnose ──────────────────────────────────────────────────────────────────

function diagnose() {
  console.log('\neacn3 diagnostics\n');
  let allOk = true;

  function check(label, fn) {
    try {
      const result = fn();
      ok(label + (result ? ` — ${result}` : ''));
      return true;
    } catch (e) {
      fail(label + ` — ${e.message}`);
      allOk = false;
      return false;
    }
  }

  // 1. Package integrity
  console.log('── Package ──');
  check('openclaw.plugin.json', () => {
    const m = JSON.parse(fs.readFileSync(path.join(PKG_ROOT, 'openclaw.plugin.json'), 'utf8'));
    return `id=${m.id} v=${m.version}`;
  });
  check('dist/index.js', () => {
    const p = path.join(PKG_ROOT, 'dist', 'index.js');
    if (!fs.existsSync(p)) throw new Error('not found — run "npm run build"');
    const stat = fs.statSync(p);
    return `${(stat.size / 1024).toFixed(1)} KB, modified ${stat.mtime.toISOString().slice(0, 19)}`;
  });
  check('dist/server.js', () => {
    const p = path.join(PKG_ROOT, 'dist', 'server.js');
    if (!fs.existsSync(p)) throw new Error('not found — run "npm run build"');
    const stat = fs.statSync(p);
    return `${(stat.size / 1024).toFixed(1)} KB`;
  });
  check('skill files', () => {
    const dir = path.join(PKG_ROOT, 'skills');
    if (!fs.existsSync(dir)) throw new Error('skills/ not found');
    const skills = fs.readdirSync(dir).filter(d => {
      const skillMd = path.join(dir, d, 'SKILL.md');
      return fs.existsSync(skillMd);
    });
    return `${skills.length} skills (${skills.join(', ')})`;
  });

  // 2. Binaries
  console.log('\n── Binaries ──');
  check('node', () => process.version);
  check('git', () => {
    return execFileSync('git', ['--version'], { encoding: 'utf8', timeout: 5000 }).trim();
  });

  // 3. OpenClaw installation
  console.log('\n── OpenClaw Integration ──');
  check('extensions directory', () => {
    if (!fs.existsSync(EXT_DIR)) throw new Error(`${EXT_DIR} not found — run "npx eacn3 setup"`);
    const files = fs.readdirSync(EXT_DIR);
    return `${files.length} entries in ${EXT_DIR}`;
  });
  check('dist/index.js in extensions', () => {
    const p = path.join(EXT_DIR, 'dist', 'index.js');
    if (!fs.existsSync(p)) throw new Error('not found — run "npx eacn3 setup" to copy');
    return 'exists';
  });
  check('openclaw.json plugin entry', () => {
    if (!fs.existsSync(CONFIG_PATH)) throw new Error(`${CONFIG_PATH} not found`);
    const config = readJSON(CONFIG_PATH);
    const entry = config?.plugins?.entries?.[PLUGIN_ID];
    if (!entry) throw new Error(`no "${PLUGIN_ID}" entry in plugins.entries`);
    if (!entry.enabled) throw new Error('plugin is disabled');
    return 'enabled';
  });
  check('plugins.allow whitelist', () => {
    const config = readJSON(CONFIG_PATH);
    const allow = config?.plugins?.allow;
    if (!Array.isArray(allow)) throw new Error('plugins.allow is missing or not an array');
    if (!allow.includes(PLUGIN_ID)) throw new Error(`"${PLUGIN_ID}" not in plugins.allow`);
    return `[${allow.join(', ')}]`;
  });
  check('plugins.installs metadata', () => {
    const config = readJSON(CONFIG_PATH);
    const inst = config?.plugins?.installs?.[PLUGIN_ID];
    if (!inst) throw new Error('no install entry — run "npx eacn3 setup"');
    return `source=${inst.source} v=${inst.version} @ ${inst.installedAt}`;
  });
  check('skills.entries registration', () => {
    const config = readJSON(CONFIG_PATH);
    const skills = config?.skills?.entries;
    if (!skills) throw new Error('skills.entries missing');
    const eacn3Skills = Object.keys(skills).filter(k => k.startsWith('eacn3-') && skills[k]?.enabled);
    if (eacn3Skills.length === 0) throw new Error('no eacn3 skills enabled');
    return `${eacn3Skills.length} enabled: ${eacn3Skills.join(', ')}`;
  });
  check('skill files in extensions', () => {
    const skillsDir = path.join(EXT_DIR, 'skills');
    if (!fs.existsSync(skillsDir)) throw new Error(`${skillsDir} not found`);
    const skills = fs.readdirSync(skillsDir).filter(d =>
      fs.existsSync(path.join(skillsDir, d, 'SKILL.md'))
    );
    return `${skills.length} skills: ${skills.join(', ')}`;
  });
  check('manifest skill paths resolve', () => {
    const extManifest = path.join(EXT_DIR, 'openclaw.plugin.json');
    if (!fs.existsSync(extManifest)) throw new Error('no manifest in extensions');
    const m = JSON.parse(fs.readFileSync(extManifest, 'utf8'));
    for (const sp of m.skills || []) {
      const resolved = path.resolve(EXT_DIR, sp);
      if (!fs.existsSync(resolved)) throw new Error(`"${sp}" resolves to ${resolved} — not found`);
    }
    return (m.skills || []).join(', ');
  });

  // Summary
  console.log('');
  if (allOk) {
    console.log('  All checks passed.\n');
  } else {
    console.log('  Some checks failed. Fix the issues above and re-run:\n');
    console.log('    npx eacn3 diagnose\n');
  }

  return allOk;
}

// ── setup ─────────────────────────────────────────────────────────────────────

function setupOpenclaw() {
  console.log('\neacn3 setup\n');

  // 1. Check dist exists
  const distSrc = path.join(PKG_ROOT, 'dist', 'index.js');
  if (!fs.existsSync(distSrc)) {
    fail('dist/index.js not found — building now...');
    const build = spawnSync('npm', ['run', 'build'], { cwd: PKG_ROOT, stdio: 'inherit' });
    if (build.status !== 0) {
      fail('build failed — cannot continue');
      process.exit(1);
    }
    ok('build succeeded');
  } else {
    ok(`dist/index.js exists`);
  }

  // 2. Copy plugin files (no node_modules — resolved from package root at runtime)
  log(`copying to ${EXT_DIR} ...`);
  fs.mkdirSync(EXT_DIR, { recursive: true });

  // Copy dist/
  copyDirRecursive(path.join(PKG_ROOT, 'dist'), path.join(EXT_DIR, 'dist'));
  ok('dist/ copied');

  // Copy skills/
  const skillsSrc = path.join(PKG_ROOT, 'skills');
  if (fs.existsSync(skillsSrc)) {
    copyDirRecursive(skillsSrc, path.join(EXT_DIR, 'skills'));
    ok('skills/ copied');
  }

  // Copy manifest
  const manifestSrc = path.join(PKG_ROOT, 'openclaw.plugin.json');
  if (fs.existsSync(manifestSrc)) {
    fs.copyFileSync(manifestSrc, path.join(EXT_DIR, 'openclaw.plugin.json'));
    ok('openclaw.plugin.json copied');
  }

  // Copy package.json (needed for metadata, but NOT node_modules)
  fs.copyFileSync(path.join(PKG_ROOT, 'package.json'), path.join(EXT_DIR, 'package.json'));
  ok('package.json copied');

  // Symlink node_modules so in-process require() resolves dependencies
  // without duplicating the entire dependency tree
  const nmDst = path.join(EXT_DIR, 'node_modules');
  const nmSrc = path.join(PKG_ROOT, 'node_modules');
  if (fs.existsSync(nmDst)) {
    fs.rmSync(nmDst, { recursive: true, force: true });
  }
  if (fs.existsSync(nmSrc)) {
    fs.symlinkSync(nmSrc, nmDst, 'junction');
    ok(`node_modules → ${nmSrc} (symlink)`);
  }

  // Clean up stale "eacn" directory from previous installs
  const staleDir = path.join(os.homedir(), '.openclaw', 'extensions', 'eacn');
  if (staleDir !== EXT_DIR && fs.existsSync(staleDir)) {
    fs.rmSync(staleDir, { recursive: true, force: true });
    ok('removed stale extensions/eacn directory');
  }
  const skillNames = [];
  if (fs.existsSync(skillsSrc)) {
    for (const d of fs.readdirSync(skillsSrc)) {
      if (fs.existsSync(path.join(skillsSrc, d, 'SKILL.md'))) {
        skillNames.push(d);
      }
    }
  }
  ok(`found ${skillNames.length} skills: ${skillNames.join(', ')}`);

  // 4. Update openclaw.json
  log(`updating ${CONFIG_PATH} ...`);
  const config = readJSON(CONFIG_PATH);
  const pkg = readJSON(path.join(PKG_ROOT, 'package.json'));

  // plugins — clean stale "eacn" entries from previous installs
  if (!config.plugins) config.plugins = {};
  if (!Array.isArray(config.plugins.allow)) config.plugins.allow = [];
  config.plugins.allow = config.plugins.allow.filter(id => id !== 'eacn');
  if (config.plugins.entries) delete config.plugins.entries['eacn'];
  if (config.plugins.installs) delete config.plugins.installs['eacn'];

  // plugins.allow
  if (!config.plugins.allow.includes(PLUGIN_ID)) {
    config.plugins.allow.push(PLUGIN_ID);
  }
  ok(`plugins.allow: ${PLUGIN_ID} added`);

  // plugins.entries
  merge(config, {
    plugins: { entries: { [PLUGIN_ID]: { enabled: true, config: {} } } }
  });
  ok(`plugins.entries: ${PLUGIN_ID} enabled`);

  // plugins.installs
  if (!config.plugins.installs) config.plugins.installs = {};
  config.plugins.installs[PLUGIN_ID] = {
    source: 'path',
    sourcePath: PKG_ROOT,
    installPath: EXT_DIR,
    version: pkg.version || '0.5.1',
    installedAt: new Date().toISOString()
  };
  ok('plugins.installs: metadata recorded');

  // skills.entries
  if (!config.skills) config.skills = {};
  if (!config.skills.entries) config.skills.entries = {};
  for (const skill of skillNames) {
    config.skills.entries[skill] = { enabled: true };
  }
  ok(`skills.entries: ${skillNames.join(', ')} registered`);

  writeJSON(CONFIG_PATH, config);
  ok('openclaw.json written');

  // 5. Verify
  console.log('\n── Verification ──');
  const verified = diagnose();

  if (verified) {
    console.log('Setup complete. Run: openclaw gateway restart\n');
  } else {
    console.log('Setup finished with warnings — check above.\n');
  }
}

// ── setup: Claude Code ────────────────────────────────────────────────────────

function setupClaudeCode(scope) {
  console.log('\neacn3 setup — Claude Code\n');
  ensureBuild();

  let configPath;
  if (scope === 'global') {
    configPath = path.join(os.homedir(), '.claude.json');
  } else {
    configPath = path.join(process.cwd(), '.mcp.json');
  }

  const config = readJSON(configPath);
  if (!config.mcpServers) config.mcpServers = {};
  config.mcpServers.eacn3 = {
    type: 'stdio',
    command: NPX_CMD,
    args: ['eacn3', 'serve'],
  };

  writeJSON(configPath, config);
  ok(`MCP server registered in ${configPath}`);

  // Hint about AGENT_GUIDE
  const guide = agentGuidePath();
  if (fs.existsSync(guide)) {
    log('');
    log(`Tip: AGENT_GUIDE.md is at ${guide}`);
    log('Add to your CLAUDE.md or project instructions for best results:');
    log(`  "Read ${guide} before using eacn3_* tools."`);
  }

  console.log(`\nDone. Restart Claude Code to load the eacn3 MCP server.\n`);
}

// ── setup: Cursor ─────────────────────────────────────────────────────────────

function setupCursor(scope) {
  console.log('\neacn3 setup — Cursor\n');
  ensureBuild();

  let configPath;
  if (scope === 'global') {
    configPath = path.join(os.homedir(), '.cursor', 'mcp.json');
  } else {
    configPath = path.join(process.cwd(), '.cursor', 'mcp.json');
  }

  const config = readJSON(configPath);
  if (!config.mcpServers) config.mcpServers = {};
  config.mcpServers.eacn3 = {
    command: NPX_CMD,
    args: ['eacn3', 'serve'],
  };

  writeJSON(configPath, config);
  ok(`MCP server registered in ${configPath}`);

  const guide = agentGuidePath();
  if (fs.existsSync(guide)) {
    log('');
    log(`Tip: AGENT_GUIDE.md is at ${guide}`);
    log('Add to your .cursorrules for best results:');
    log(`  "Read ${guide} before using eacn3_* tools."`);
  }

  console.log(`\nDone. Restart Cursor to load the eacn3 MCP server.\n`);
}

// ── setup: Codex ──────────────────────────────────────────────────────────────

function setupCodex(scope) {
  console.log('\neacn3 setup — Codex\n');
  ensureBuild();

  let configPath;
  if (scope === 'global') {
    configPath = path.join(os.homedir(), '.codex', 'config.toml');
  } else {
    configPath = path.join(process.cwd(), '.codex', 'config.toml');
  }

  fs.mkdirSync(path.dirname(configPath), { recursive: true });

  // Read existing TOML content
  let toml = '';
  try { toml = fs.readFileSync(configPath, 'utf8'); } catch (_) {}

  // Remove existing [mcp_servers.eacn3] block if present
  toml = toml.replace(/\[mcp_servers\.eacn3\][^\[]*/, '');

  // Append new block
  const block = [
    '',
    '[mcp_servers.eacn3]',
    `command = "${NPX_CMD}"`,
    `args = ["eacn3", "serve"]`,
    'enabled = true',
    '',
  ].join('\n');

  toml = toml.trimEnd() + '\n' + block;
  fs.writeFileSync(configPath, toml, 'utf8');
  ok(`MCP server registered in ${configPath}`);

  const guide = agentGuidePath();
  if (fs.existsSync(guide)) {
    log('');
    log(`Tip: AGENT_GUIDE.md is at ${guide}`);
    log('Add to your AGENTS.md for best results:');
    log(`  "Read ${guide} before using eacn3_* tools."`);
  }

  console.log(`\nDone. Restart Codex to load the eacn3 MCP server.\n`);
}

// ── setup router ──────────────────────────────────────────────────────────────

function setupRouter() {
  const target = process.argv[3];
  const flags = process.argv.slice(4);
  const scope = flags.includes('--global') ? 'global' : 'project';

  switch (target) {
    case 'claude-code':
    case 'claude':
      setupClaudeCode(scope);
      break;
    case 'cursor':
      setupCursor(scope);
      break;
    case 'codex':
      setupCodex(scope);
      break;
    case undefined:
    case 'openclaw':
      setupOpenclaw();
      break;
    default:
      console.log(`\nUnknown target: "${target}"\n`);
      console.log('Supported targets:');
      console.log('  npx eacn3 setup                  # OpenClaw (default)');
      console.log('  npx eacn3 setup claude-code       # Claude Code');
      console.log('  npx eacn3 setup cursor            # Cursor');
      console.log('  npx eacn3 setup codex             # Codex');
      console.log('');
      console.log('Options:');
      console.log('  --global    Install to user-level config (default: project-level)');
      console.log('');
      process.exit(1);
  }
}

// ── health ────────────────────────────────────────────────────────────────────

async function healthCheck(endpoint) {
  const url = `${endpoint}/health`;
  console.log(`\nProbing ${url} ...\n`);
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(5000) });
    if (!res.ok) {
      fail(`HTTP ${res.status}`);
      process.exit(1);
    }
    const data = await res.json();
    ok(`status: ${data.status || 'ok'}`);
    console.log(JSON.stringify(data, null, 2));
  } catch (e) {
    fail(`unreachable — ${e.message}`);
    process.exit(1);
  }
}

// ── cluster ───────────────────────────────────────────────────────────────────

async function clusterStatus(endpoint) {
  const url = `${endpoint}/api/cluster/status`;
  console.log(`\nQuerying ${url} ...\n`);
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(5000) });
    if (!res.ok) {
      fail(`HTTP ${res.status}`);
      process.exit(1);
    }
    const data = await res.json();
    ok(`mode: ${data.mode}, members: ${data.member_count}, online: ${data.online_count}`);
    if (data.members) {
      for (const m of data.members) {
        const icon = m.status === 'online' ? '\u2713' : '\u2717';
        console.log(`  ${icon} ${m.node_id}  ${m.endpoint}  [${m.status}]`);
      }
    }
    if (data.seed_nodes) {
      console.log(`\n  seed nodes: ${data.seed_nodes.join(', ')}`);
    }
  } catch (e) {
    fail(`unreachable — ${e.message}`);
    process.exit(1);
  }
}

// ── help ──────────────────────────────────────────────────────────────────────

function showHelp() {
  console.log(`
eacn3 — EACN3 network plugin CLI (v${readJSON(path.join(PKG_ROOT, 'package.json')).version || '0.5.1'})

Usage:
  eacn3 <command> [target] [options]

Commands:
  serve                Start the MCP stdio server (used by client configs)
  setup [target]       Install plugin for a specific client
  diagnose | doctor    Run full diagnostics on plugin installation
  health [endpoint]    Probe a network node's /health endpoint
  cluster [endpoint]   Show cluster topology and member status

Setup targets:
  setup                OpenClaw native plugin (default)
  setup claude-code    Claude Code — writes .mcp.json or ~/.claude.json
  setup cursor         Cursor — writes .cursor/mcp.json
  setup codex          Codex — writes .codex/config.toml

Setup options:
  --global             Install to user-level config (default: project-level)

Examples:
  npx eacn3 setup                                    # OpenClaw
  npx eacn3 setup claude-code                        # Claude Code (project)
  npx eacn3 setup claude-code --global               # Claude Code (global)
  npx eacn3 setup cursor                             # Cursor (project)
  npx eacn3 setup codex --global                     # Codex (global)
  npx eacn3 diagnose
  npx eacn3 health http://175.102.130.69:37892
`);
}

// ── main ──────────────────────────────────────────────────────────────────────

const DEFAULT_ENDPOINT = process.env.EACN3_NETWORK_URL || 'https://network.eacn3.dev';
const cmd = process.argv[2];

switch (cmd) {
  case 'serve':
  case 'server':
  case 'start': {
    // Launch the MCP stdio server — used by MCP client configs
    const serverJs = path.join(PKG_ROOT, 'dist', 'server.js');
    if (!fs.existsSync(serverJs)) {
      console.error('dist/server.js not found — run "npm run build" first');
      process.exit(1);
    }
    const { execFileSync: run } = require('child_process');
    try { run(process.execPath, [serverJs], { stdio: 'inherit' }); }
    catch (e) { process.exit(e.status || 1); }
    break;
  }
  case 'setup':
    setupRouter();
    break;
  case 'diagnose':
  case 'diag':
  case 'doctor':
    diagnose();
    break;
  case 'health':
    healthCheck(process.argv[3] || DEFAULT_ENDPOINT);
    break;
  case 'cluster':
    clusterStatus(process.argv[3] || DEFAULT_ENDPOINT);
    break;
  case '--help':
  case '-h':
  case 'help':
    showHelp();
    break;
  default:
    showHelp();
    process.exit(cmd ? 1 : 0);
}
