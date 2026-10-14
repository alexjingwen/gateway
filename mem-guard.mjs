#!/usr/bin/env node
// mem-guard.mjs —— 设备级内存守卫（**ollama-token-gateway 的运维侧工具**）。
//
// 解决什么：
//   florr 挂机 + 浏览器 + 各类 node 常驻进程长时间跑，内存只涨不跌。等到真 OOM 才崩，
//   现场已经没了。这个工具在**设备内存使用率触及上限前**就报警 / 采取行动，把"事后救火"
//   变成"事前预警"。
//
// 监控口径（macOS，对齐「活动监视器 → 内存」）：
//   使用量 = 匿名页(Anonymous) + 连线内存(Wired) + 压缩占用(Compressor occupied)
//   上限   = hw.memsize（整台设备物理内存）
//   使用率 = 使用量 / 上限
//   ⚠️ 这是「活动监视器口径」，不是 free 反推（free 把 inactive/purgeable 也算闲，会低估占用）。
//
// 三种运行模式（CLI）：
//   node mem-guard.mjs --sample           一次采样，打印 JSON
//   node mem-guard.mjs --watch 30        每 30s 采样一次，越线即触发 onBreach
//   node mem-guard.mjs --selftest         不碰真机，用合成 vm_stat 验证统计学（回归用）
//
// 越线动作（--at 默认 0.9 = 90%）：
//   默认：打印告警 + 追加 JSONL 到 mem-guard.log（不改任何进程行为，纯观察）
//   --cmd "..."    ：越线时执行该命令（例如重启网关，需自己保证幂等）
//   --webhook URL  ：越线时 POST 一条 JSON 告警到该 URL
//   --fail-open / --no-fail-open ：
//       读不到内存（非 macOS / 沙箱）时，fail-open（默认）静默放行不报错；
//       --no-fail-open 会直接抛错，便于 CI 里显式失败。
//
// 设计取舍：
//   - 只依赖 node 内置（child_process / fs），不装任何包；macOS 用 sysctl + vm_stat 原生读数。
//   - 越线保护是"观察 + 可选动作"，**不**替网关决定业务策略——网关自己的限流逻辑在网关侧。
//   - 全程 fail-soft：探针不可用时绝不把调用方搞崩。

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export class MemoryProbeError extends Error {
  constructor(msg) {
    super(msg);
    this.name = "MemoryProbeError";
  }
}

export class MemoryGuardError extends Error {
  constructor(msg, reading) {
    super(msg);
    this.name = "MemoryGuardError";
    this.reading = reading;
  }
}

/**
 * 读一次设备内存（macOS）。
 * @returns {{totalBytes:number,usedBytes:number,freeBytes:number,pct:number,
 *            appBytes:number,wiredBytes:number,compressedBytes:number,pageBytes:number,method:string}}
 */
export function readDeviceMemory() {
  let totalRaw;
  try {
    totalRaw = execFileSync("sysctl", ["-n", "hw.memsize"], { encoding: "utf8" }).trim();
  } catch (e) {
    throw new MemoryProbeError(`sysctl hw.memsize 失败：${e.message}`);
  }
  const totalBytes = Number(totalRaw);
  if (!Number.isFinite(totalBytes) || totalBytes <= 0) {
    throw new MemoryProbeError(`hw.memsize 读数异常：${totalRaw}`);
  }

  let vm;
  try {
    vm = execFileSync("vm_stat", [], { encoding: "utf8" });
  } catch (e) {
    throw new MemoryProbeError(`vm_stat 失败：${e.message}`);
  }

  // 页大小：vm_stat 首行形如 "Mach Virtual Memory Statistics: (page size of 16384 bytes)"
  const psz = vm.match(/page size of\s+(\d+)\s+bytes/i);
  const pageBytes = psz ? Number(psz[1]) : 4096;

  const counters = {};
  for (const line of vm.split("\n")) {
    // 注意：macOS vm_stat 里 "Anonymous pages:" **没有** "Pages" 前缀，
    //       其余大多是 "Pages xxx:"，所以用可选前缀 (?:Pages\s+)?。
    const m = line.match(/^(?:Pages\s+)?([\w-]+(?:\s+[\w-]+)*?)\s*:\s*(\d+)/);
    if (!m) continue;
    const key = m[1].replace(/\s+/g, "_").replace(/_pages$/, "").replace(/_+$/, "").toLowerCase();
    counters[key] = Number(m[2]);
  }

  const anon = counters["anonymous"] ?? 0;
  const wired = counters["wired_down"] ?? 0;
  const compressed = counters["occupied_by_compressor"] ?? 0;
  const free = counters["free"] ?? 0;

  const usedBytes = (anon + wired + compressed) * pageBytes;
  const freeBytes = free * pageBytes;
  const pct = totalBytes > 0 ? usedBytes / totalBytes : 0;

  return {
    totalBytes,
    usedBytes,
    freeBytes,
    pct,
    appBytes: anon * pageBytes,
    wiredBytes: wired * pageBytes,
    compressedBytes: compressed * pageBytes,
    pageBytes,
    method: "macos-anon+wired+compressed",
  };
}

/**
 * 建一个内存守卫。
 * @param {object} opts
 * @param {number} [opts.at=0.9]        触发阈值（占上限比例）
 * @param {number} [opts.capBytes=null] 上限字节；null=整台设备物理内存
 * @param {boolean}[opts.failOpen=true] 探针不可用时是否静默放行
 * @param {number} [opts.cooldownMs=30000] 同一越线状态的最小重复间隔
 * @param {string} [opts.logFile]       越线 JSONL 追加路径
 * @param {string} [opts.cmd]           越线时执行的命令
 * @param {string} [opts.webhook]       越线时 POST 的 URL
 * @param {function}[opts.onBreach]     越线回调 (reading)=>void
 * @param {function}[opts.probe]        自定义探针（测试用）
 */
export function createMemoryGuard(opts = {}) {
  const at = opts.at ?? 0.9;
  const capBytes = opts.capBytes ?? null;
  const failOpen = opts.failOpen ?? true;
  const cooldownMs = opts.cooldownMs ?? 30000;
  const probe = opts.probe ?? readDeviceMemory;

  let last = null;
  let breached = false;
  let breachedAt = 0;
  let available = true;
  let initError = null;

  function sample() {
    try {
      last = probe();
      initError = null;
      available = true;
    } catch (e) {
      initError = e;
      available = false;
      if (!failOpen) throw e; // fail-closed：探针挂了就显式失败
    }
    return last;
  }

  function breachMessage(reading) {
    const cap = capBytes ?? reading.totalBytes;
    const usedPct = ((reading.usedBytes / cap) * 100).toFixed(1);
    return `内存守卫：设备使用率 ${usedPct}% ≥ 上限 ${ (at * 100).toFixed(0) }%（使用 ${(reading.usedBytes / 1073741824).toFixed(1)}GiB / 上限 ${(cap / 1073741824).toFixed(1)}GiB）`;
  }

  async function fire(reading) {
    const msg = breachMessage(reading);
    if (opts.onBreach) {
      try {
        await opts.onBreach(reading, msg);
      } catch (e) {
        console.error(`[mem-guard] onBreach 回调出错（不影响探针）：${e.message}`);
      }
    }
    console.warn(`[mem-guard] ${msg}`);
    if (opts.logFile) {
      try {
        fs.appendFileSync(
          opts.logFile,
          JSON.stringify({ t: new Date().toISOString(), pct: reading.pct, usedBytes: reading.usedBytes, totalBytes: reading.totalBytes, at, msg }) + "\n"
        );
      } catch (e) {
        console.error(`[mem-guard] 写日志失败：${e.message}`);
      }
    }
    if (opts.webhook) {
      try {
        await fetch(opts.webhook, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ alert: "memory-guard", pct: reading.pct, usedBytes: reading.usedBytes, totalBytes: reading.totalBytes, at, msg, at_iso: new Date().toISOString() }),
        });
      } catch (e) {
        console.error(`[mem-guard] webhook 投递失败：${e.message}`);
      }
    }
    if (opts.cmd) {
      try {
        execFileSync(opts.cmd, [], { shell: true, stdio: "inherit", timeout: 60000 });
      } catch (e) {
        console.error(`[mem-guard] 越线命令执行失败：${e.message}`);
      }
    }
  }

  /**
   * 采样并判定。返回 { available, breached, pct, reading }。
   */
  function check() {
    const reading = sample();
    if (!available) {
      return { available: false, breached: false, pct: null, reading: null, error: String(initError?.message) };
    }
    const cap = capBytes ?? reading.totalBytes;
    const pct = reading.usedBytes / cap;
    const now = Date.now();
    if (pct >= at) {
      if (!breached || now - breachedAt >= cooldownMs) {
        breached = true;
        breachedAt = now;
        // fire 是异步的，但不阻塞 check 的返回（告警迟到不影响采样频率）
        fire(reading);
      }
    } else {
      breached = false;
    }
    return { available: true, breached, pct, reading };
  }

  sample(); // 启动时探一次，尽早暴露探针故障

  return {
    at,
    get available() {
      return available;
    },
    get breached() {
      return breached;
    },
    get reading() {
      return last;
    },
    sample,
    check,
  };
}

// ─────────────────────────── CLI ───────────────────────────

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--sample") out.sample = true;
    else if (a === "--selftest") out.selftest = true;
    else if (a === "--watch") out.watch = Number(argv[++i]) || 30;
    else if (a === "--at") out.at = Number(argv[++i]);
    else if (a === "--cmd") out.cmd = argv[++i];
    else if (a === "--webhook") out.webhook = argv[++i];
    else if (a === "--log") out.log = argv[++i];
    else if (a === "--fail-open") out.failOpen = true;
    else if (a === "--no-fail-open") out.failOpen = false;
    else out._.push(a);
  }
  return out;
}

/** 合成 vm_stat 文本（测试用，不碰真机）。 */
function syntheticVmStat({ pageBytes = 4096, free = 1000000, active = 200000, inactive = 150000, speculative = 10000, wired = 50000, purgeable = 2000, compressorStored = 80000, compressorOccupied = 40000, anon = 300000, filebacked = 120000 } = {}) {
  return [
    `Mach Virtual Memory Statistics: (page size of ${pageBytes} bytes)`,
    `Pages free:                               ${free}.`,
    `Pages active:                             ${active}.`,
    `Pages inactive:                           ${inactive}.`,
    `Pages speculative:                        ${speculative}.`,
    `Pages wired down:                         ${wired}.`,
    `Pages purgeable:                          ${purgeable}.`,
    `Pages stored in compressor:               ${compressorStored}.`,
    `Pages occupied by compressor:             ${compressorOccupied}.`,
    `Anonymous pages:                          ${anon}.`,
    `File-backed pages:                        ${filebacked}.`,
  ].join("\n");
}

/** 回归自测：用合成数据校验统计学，不依赖真机。 */
function runSelfTest() {
  const pageBytes = 4096;
  const totalBytes = 16 * 1024 * 1024 * 1024; // 16 GiB
  const vm = syntheticVmStat({ pageBytes });
  // 复刻 readDeviceMemory 的解析，但用合成输入
  const psz = vm.match(/page size of\s+(\d+)\s+bytes/i);
  const pb = psz ? Number(psz[1]) : 4096;
  const counters = {};
  for (const line of vm.split("\n")) {
    const m = line.match(/^(?:Pages\s+)?([\w-]+(?:\s+[\w-]+)*?)\s*:\s*(\d+)/);
    if (!m) continue;
    const key = m[1].replace(/\s+/g, "_").replace(/_pages$/, "").replace(/_+$/, "").toLowerCase();
    counters[key] = Number(m[2]);
  }
  const anon = counters["anonymous"];
  const wired = counters["wired_down"];
  const compressed = counters["occupied_by_compressor"];
  const used = (anon + wired + compressed) * pb;
  const pct = used / totalBytes;

  const expectUsed = (300000 + 50000 + 40000) * 4096; // 390000 * 4096
  const expectPct = expectUsed / totalBytes;

  const ok = Math.abs(used - expectUsed) < 1 && Math.abs(pct - expectPct) < 1e-9;
  console.log(JSON.stringify({ pageBytes: pb, anon, wired, compressed, usedBytes: used, totalBytes, pct: +pct.toFixed(6), expectUsedBytes: expectUsed, expectPct: +expectPct.toFixed(6), pass: ok }, null, 2));
  if (!ok) {
    console.error("[mem-guard] 自测失败：统计与预期不符");
    process.exit(1);
  }
  console.log("[mem-guard] 自测通过 ✅");
  process.exit(0);
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.selftest) return runSelfTest();

  if (args.sample) {
    try {
      const r = readDeviceMemory();
      console.log(JSON.stringify({ ...r, pct: +r.pct.toFixed(4), usedGiB: +(r.usedBytes / 1073741824).toFixed(2), totalGiB: +(r.totalBytes / 1073741824).toFixed(2) }, null, 2));
    } catch (e) {
      console.error(`[mem-guard] 采样失败：${e.message}`);
      process.exit(args.failOpen === false ? 1 : 0);
    }
    return;
  }

  if (args.watch) {
    const guard = createMemoryGuard({
      at: args.at ?? 0.9,
      failOpen: args.failOpen ?? true,
      cmd: args.cmd,
      webhook: args.webhook,
      logFile: args.log ?? path.join(__dirname, "mem-guard.log"),
      onBreach: (r, msg) => {
        // 默认 onBreach 之外的钩子点；告警已在 fire() 里打过，这里留空可被覆盖
      },
    });
    console.log(`[mem-guard] 开始监控：每 ${args.watch}s 采样，阈值 ${( (args.at ?? 0.9) * 100).toFixed(0) }%（Ctrl+C 退出）`);
    const timer = setInterval(() => {
      const res = guard.check();
      if (res.available) {
        const gi = (res.reading.usedBytes / 1073741824).toFixed(2);
        const tot = (res.reading.totalBytes / 1073741824).toFixed(2);
        console.log(`[mem-guard] ${(res.pct * 100).toFixed(1)}%  (${gi}/${tot} GiB)${res.breached ? "  ⚠️ 已越线" : ""}`);
      } else {
        console.warn(`[mem-guard] 探针不可用：${res.error}`);
      }
    }, args.watch * 1000);
    timer.unref?.();
    // 不让进程立即退出：setInterval 已持有事件循环；这里保持运行
    return;
  }

  // 无子命令：默认给一次采样
  try {
    const r = readDeviceMemory();
    console.log(JSON.stringify({ pct: +r.pct.toFixed(4), usedGiB: +(r.usedBytes / 1073741824).toFixed(2), totalGiB: +(r.totalBytes / 1073741824).toFixed(2), method: r.method }, null, 2));
  } catch (e) {
    console.error(`[mem-guard] ${e.message}`);
    process.exit(args.failOpen === false ? 1 : 0);
  }
}

// 直接运行时执行 CLI；被 import 时不自动跑
if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}
