/* dirtidy 控制台：扫描 / 审阅 / 执行 / 隔离区 / 规则 / 设置 / 监视
 *
 * 所有动作都打到后端同一份实现（pipeline / quarantine / ai / watch），
 * 网页不自己算规则、不自己判断保护——否则「网页说的和命令行做的不一样」。
 */

let currentRun = null;
const decisions = new Map();
/* 本轮模型建议（path → suggestion）。只用来「显示建议」和「按建议采纳」，
   绝不自动写进 decisions——默认决策恒为 keep。 */
let currentSuggestions = {};
/* 本轮「执行前预览」的结果（后端 dry-run 原样返回）。
 * **这是执行页所有数字的唯一来源**——不要再用 `pending_auto_hits.length`
 * 去当「将搬走几项」：那是「规则命中几条」，不是「实际会搬几条」。
 * 两者不等的情形至少两类，且都是常态：
 *   - 目录命中覆盖其内的文件命中（只搬父目录）；
 *   - 命中目录的子树里含受保护路径（整条被拦下）。
 * 实测夹具：命中 4 条，实际只搬 2 条。拿前者报数就是在报假账。 */
let currentPlan = null;
/* 审阅时勾选「保留」的自动隔离项：执行自动隔离时随请求发出，本轮不动它们 */
const keptPaths = new Set();
/* 隔离区里勾选要还原/彻底删除的条目 id */
const quarantined = new Set();
/* 候选表批量选择：只记「勾了哪些路径」。它不参与任何判定，
   也不影响执行范围——真正决定搬什么的是 decisions。 */
const candPicked = new Set();
/* 规则页：待保存的直删规则 id 与已保存的，用于标出改动 */
let rulePurge = new Set();
let savedPurge = new Set();
let currentPack = "";
let watchTimer = null;
/* 最近一次 /api/runs/{id} 的响应。筛选、批量勾选「保留」之后要按同一份数据重渲染，
   否则得再请求一次接口——而重渲染只是把 DOM 按已有状态重画，不必联网。 */
let currentRunData = null;
let currentCandShown = [];
let currentCandMatching = [];
const REVIEW_PAGE_SIZE = 100;
let pendingPage = 1;
let candPage = 1;
let scanPollTimer = null;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = await r.text();
    try {
      const parsed = JSON.parse(detail);
      if (parsed && parsed.detail) detail = parsed.detail;
    } catch (_e) {
      /* 非 JSON 错误体就原样展示 */
    }
    throw new Error(detail);
  }
  return r.json();
}

/* 后端返回的字段名 → 界面上的人话。没登记的键原样显示（不隐藏信息，只是不加戏）。 */
const STATUS_LABELS = {
  saved: "已保存判定",
  kept: "保留",
  archived: "归档",
  quarantined: "隔离",
  purge_refused: "未获准直删（只隔离）",
  purged_paths: "已直删路径",
  dry_run: "仅预览（未动文件）",
  would_quarantine: "预计隔离",
  would_purge: "预计直删（不可还原）",
  would_archive: "预计归档",
  blocked_paths: "保护拦下路径",
  kept_paths: "人工保留路径",
  paths: "预计搬走路径",
  would_purge_paths: "预计直删路径",
  purged_directly: "直删",
  purged: "彻底删除",
  restored: "还原",
  count: "共",
  suggestions: "模型建议",
  run_id: "run",
  blocked_by_protect: "受保护拦下",
  kept_by_user: "人工保留",
  skipped: "跳过",
  auto_purge_rules: "直删规则",
  use_model: "启用模型",
  adopted: "已按建议填入",
  applied: "已按设置执行自动隔离",
  auto_hits: "规则命中",
  running: "运行中",
  stopped: "已发出停止信号",
  configured: "已配置",
  provider: "接口",
  model: "模型",
};

let statusTimer = 0;

/* 状态条：只显示人话。对象会折成「隔离 3　直删 1」这类摘要，
   不再把裸 JSON 铺到页面上。错误常驻直到下次操作或手动关闭，
   普通提示 6 秒后自动收起，免得占着视线。 */
function setStatus(obj, opts = {}) {
  const bar = document.getElementById("statusbar");
  const text = document.getElementById("status");
  if (!bar || !text) return;
  const isError = opts.error === true || obj instanceof Error;
  // 转移摘要：先显示「搬走多少」，等 loadRun 完成后 finishTransferStatus
  // 再补上「还剩多少」——两步走，否则数字只有一半。
  if (opts.transfer && !isError) {
    const head = transferSummary(obj).length
      ? `✓ ${transferSummary(obj).join("，")}`
      : humanStatus(obj);
    text.textContent = head;
  } else {
    text.textContent = isError ? String((obj && obj.message) || obj) : humanStatus(obj);
    // 非转移动作（保存、采纳、批量设决策等）一旦写状态条，上一次的转移摘要
    // 就该作废——否则下次 loadRun 会误把它当成「刚做完的动作」再补一句「还剩多少」。
    lastTransferSummary = "";
  }
  bar.classList.toggle("error", isError);
  bar.hidden = false;
  window.clearTimeout(statusTimer);
  if (!isError) {
    statusTimer = window.setTimeout(() => {
      bar.hidden = true;
    }, 6000);
  }
}

/* ------------------------- 按钮的「正在做」状态 -------------------------
   动作慢的时候（扫描、搬几百个文件），按钮若只是静默等着，用户会以为没点上，
   于是再点一次——而这些都是会真动文件的按钮。所以进入忙态时按钮自己会说话：
   禁用 + aria-busy + 文案后缀，转圈交给 CSS（`button[aria-busy="true"]::after`）。

   两个细节必须留神，否则忙态会比没忙态更糟：
   1. **渲染可能中途接管这个按钮**。执行完 loadRun 会重画执行页，
      `renderExecute` 会按最新计划改写「执行自动隔离（3 项）」并为它设 disabled。
      这时忙态收尾若强行还原文案与可用性，就会出现「文案说无可执行项、按钮却亮着可点」。
      故收尾时若文案已被改过，就直接让给渲染。
   2. **抛错也要收尾**（放在 finally 里），否则一次失败的请求会把按钮永久按死。 */

function busyOn(btn, label) {
  if (!btn || btn.dataset.busy === "1") return false; // 已在忙 → 忽略重复点击
  btn.dataset.busyLabel = btn.textContent;
  btn.dataset.busy = "1";
  btn.setAttribute("aria-busy", "true");
  btn.disabled = true;
  if (label) {
    btn.dataset.busyText = label;
    btn.textContent = label;
  }
  return true;
}

function busyOff(btn) {
  if (!btn || btn.dataset.busy !== "1") return;
  const original = btn.dataset.busyLabel || "";
  const shown = btn.dataset.busyText || "";
  const takenOver = Boolean(shown) && btn.textContent !== shown;
  delete btn.dataset.busyLabel;
  delete btn.dataset.busyText;
  delete btn.dataset.busy;
  btn.removeAttribute("aria-busy");
  if (takenOver) return; // 渲染已接管文案与可用性，别再抢回去
  if (original) btn.textContent = original;
  btn.disabled = false;
}

/* 包一层：进忙态 → 执行 → 无论如何收尾。 */
async function withBusy(btn, label, fn) {
  if (!busyOn(btn, label)) return undefined;
  try {
    return await fn();
  } finally {
    busyOff(btn);
  }
}

function humanStatus(obj) {
  if (obj == null) return "";
  if (typeof obj === "string") return obj;
  if (typeof obj !== "object") return String(obj);
  const bits = [];
  for (const [k, v] of Object.entries(obj)) {
    if (v == null || v === "" || (Array.isArray(v) && v.length === 0)) continue;
    const label = STATUS_LABELS[k] || k;
    if (Array.isArray(v)) bits.push(`${label} ${v.length} 条`);
    else if (typeof v === "object") continue;
    else if (typeof v === "boolean") {
      if (v) bits.push(label); // 布尔用「只报真的那个」：显示「已执行隔离」，而不是「已执行隔离开」
    } else bits.push(`${label} ${v}`);
  }
  return bits.join("　") || "完成";
}

/* --------------------- 状态转移后的统计汇总摘要 ---------------------
   执行隔离 / 候选决策 / 还原 / 彻底删除 / 采纳建议等动作做完后，
   用户要的不只是「隔离 3」这种零散字段，而是「搬走了多少、留下多少、
   拦下多少、当前还剩多少、隔离区累计多少」的一整句话。
   这里不重新算任何判定——数字全部来自后端返回体，前端只负责说人话。 */

/* 后端动作返回体里的字段名 → 人话。与 STATUS_LABELS 分开：
   这是「转移发生了多少」的动词，不是「这是个什么字段」的名词。 */
const TRANSFER_WORDS = {
  quarantined: "搬进隔离区",
  purged_directly: "直删",
  purged: "彻底删除",
  restored: "还原",
  archived: "归档",
  kept: "保留",
  kept_by_user: "人工保留",
  blocked_by_protect: "受保护拦下",
  skipped: "跳过",
  adopted: "填入决策",
  purge_refused: "未获准直删（只隔离）",
};

function transferSummary(obj) {
  if (!obj || typeof obj !== "object") return "";
  const done = [];
  for (const k of Object.keys(TRANSFER_WORDS)) {
    const raw = obj[k];
    const v = Array.isArray(raw) ? raw.length : raw;
    if (typeof v === "number" && v > 0) done.push(`${TRANSFER_WORDS[k]} ${v} 项`);
  }
  return done;
}

/* 操作完成后、loadRun 重载前先记住一次结果，待新数据到位后补上
   「当前待处理 / 隔离区累计」，合起来才是完整的「转移摘要」。 */
let lastTransferSummary = "";

function transferStatus(res) {
  const parts = transferSummary(res);
  const head = parts.length ? `✓ ${parts.join("，")}` : "";
  lastTransferSummary = head;
  if (!head) {
    setStatus(res);
    return;
  }
  setStatus(res, { transfer: true });
}

/* loadRun 之后调用：把「搬走了多少」与「现在还剩多少」拼成一句话。 */
function finishTransferStatus() {
  if (!lastTransferSummary) return;
  const tail = transferAftermath();
  setStatus(tail ? `${lastTransferSummary}。${tail}` : lastTransferSummary);
  lastTransferSummary = "";
}

/* 从当前载入的数据里算「现在还剩多少」：待处理项 + 隔离区累计。
   纯显示，不碰任何判定。 */
function transferAftermath() {
  const d = currentRunData;
  if (!d) return "";
  const pending = (d.pending_auto_hits || []).length;
  const cand = (d.candidates || []).length;
  const q = (d.quarantine_summary || {}).count || 0;
  const qsize = (d.quarantine_summary || {}).size || 0;
  const bits = [];
  if (pending + cand) bits.push(`当前还有 ${pending + cand} 项待处理`);
  else bits.push("待处理项已清空");
  if (q) bits.push(`隔离区累计 ${q} 项（${humanSize(qsize)}）`);
  return bits.join("，");
}

function clear(node) {
  node.replaceChildren();
}

/* 空表提示：往 tbody 塞一行占满整行的说明。
   只有表头、底下什么都没有，会让人以为是加载失败或者页面坏了。 */
function emptyRow(tbody, colspan, text) {
  const tr = document.createElement("tr");
  const td = el("td", { cls: "empty-note", text });
  td.setAttribute("colspan", String(colspan));
  tr.appendChild(td);
  tbody.appendChild(tr);
}

/* 统一用 DOM API 建节点：文件名/路径一律走 textContent 或 setAttribute，
   绝不拼 HTML 字符串，避免恶意文件名（如从压缩包解出的文件）注入脚本。 */
function el(tag, { text, cls, attrs, on } = {}) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = String(text);
  if (cls) node.className = cls;
  if (attrs) for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (on) for (const [evt, fn] of Object.entries(on)) node.addEventListener(evt, fn);
  return node;
}

function humanSize(n) {
  const v = Number(n) || 0;
  if (v <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let x = v;
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024;
    i += 1;
  }
  return `${i === 0 ? x : x.toFixed(x < 10 ? 1 : 0)} ${units[i]}`;
}

/* 占用大小的统一出口：后端算不出来时给 `-1`，显示「—」而不是「0 B」——
   「0 B」会被读成「这是个空文件」，而实际只是没算（条目太多时整批不算）。 */
function sizeText(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v < 0) return "—";
  return humanSize(v);
}

function card(k, v, s) {
  const box = el("div", { cls: "card" });
  box.appendChild(el("div", { cls: "k", text: k }));
  box.appendChild(el("div", { cls: "v", text: v }));
  if (s) box.appendChild(el("div", { cls: "s", text: s }));
  return box;
}

/* 决策选项：value 是后端认识的三个动作，显示成人话。
   默认恒为 `keep`（保留不动）——模型建议**不会**自动变成决策，
   这是「自动只到候选为止、执行必须人工」在界面上的落点。 */
const DECISION_LABELS = {
  keep: "保留不动",
  quarantine: "隔离",
  archive: "归档",
};

/* 规则动作的中文说法。规则页原先直接铺 `auto_quarantine` 这种内部词，
   而它决定的是「这行会不会被自动搬走」——正是用户最该看懂的一列。
   措辞取短（列窄，长句子会折成三行），详细含义放 title。 */
const ACTION_LABELS = {
  protect: "保护",
  auto_quarantine: "自动隔离",
  candidate_only: "只进候选",
};
const ACTION_HINTS = {
  protect: "永不自动隔离，最高优先级",
  auto_quarantine: "进待隔离清单，执行时搬入隔离区（可还原）",
  candidate_only: "只进候选，交人工判断，不会自动动",
};

/* 建议标签。判定在 `dirtidy/advice.py`，这里只负责好看。
   刻意**不在前端按 rule_id 拼措辞**：那会变成第二份判定，规则包一改名或加
   `purge_safe`，界面就会说「可以清理」而实际执行是「直接删除」——本项目
   已经四次栽在「同一个判定写两份」上（详见 docs/06）。 */
let ADVICE_KINDS = [];

function adviceTag(entry) {
  const advice = ADVICE_KINDS.includes(entry.advice) ? entry.advice : "review";
  const hint = entry.advice_hint || "";
  return el("span", {
    cls: `advice advice-${advice}`,
    text: entry.advice_label || "需要你判断",
    attrs: hint ? { title: hint } : {},
  });
}

/* 规则的中文短名（规则包里的 `label`）。
   没有 label 就回落到 rule_id——排查时还得看得到内部编号，
   但它不该是用户看到的第一信息（`dep_node_modules` 只能靠猜）。 */
function ruleLabel(entry) {
  if (!entry) return "";
  return entry.rule_label || entry.rule_id || "";
}

function decisionFor(path) {
  return decisions.get(path) || { path, action: "keep", target_relpath: "", note: "" };
}

/* 「为什么？」按钮：弹窗展示该路径的规则判定明细。
   后端直接返回 CLI explain 的 to_text()，所以网页与命令行看到的是同一份解释。 */
function whyButton(path) {
  const btn = el("button", {
    cls: "why",
    text: "为什么？",
    attrs: { type: "button", title: "看这条路径的规则判定明细" },
  });
  btn.addEventListener("click", () => explain(path));
  return btn;
}

async function explain(path) {
  const dlg = document.getElementById("explainDlg");
  document.getElementById("explainPath").textContent = path;
  document.getElementById("explainSummary").textContent = "";
  document.getElementById("explainBody").textContent = "加载中…";
  dlg.showModal();
  try {
    const data = await api(
      `/api/runs/${currentRun}/explain?path=${encodeURIComponent(path)}`,
    );
    renderExplainSummary(data.summary);
    document.getElementById("explainBody").textContent = data.text;
  } catch (e) {
    document.getElementById("explainSummary").textContent = "";
    document.getElementById("explainBody").textContent = String(e);
  }
}

/* 四段人话结论：后端 human_summary 给什么就显示什么，不在这里拼措辞——
   同一个语义散两处迟早漂移（本项目已四次栽在「同一个判定写两份」上）。 */
function renderExplainSummary(summary) {
  const box = document.getElementById("explainSummary");
  clear(box);
  if (!summary) return;
  const rows = [
    ["这是什么", summary.what],
    ["系统会怎么办", summary.action],
    ["为什么", summary.why],
    ["删了怎么办", summary.recover],
  ];
  for (const [k, v] of rows) {
    if (!v) continue;
    const line = el("div", { cls: "explain-row" });
    line.appendChild(el("div", { cls: "explain-k", text: k }));
    line.appendChild(el("div", { cls: "explain-v", text: v }));
    box.appendChild(line);
  }
}

/* ------------------------------- 分页导航 ------------------------------- */

function showTab(name) {
  for (const b of document.querySelectorAll(".tabbtn")) {
    b.setAttribute("aria-selected", String(b.dataset.tab === name));
  }
  for (const s of document.querySelectorAll(".tab")) {
    s.classList.toggle("active", s.id === `tab-${name}`);
  }
  // 切页回到内容顶部：`.app-main` 是滚动容器，切页时若保留上一次的滚动位置，
  // 新页会从半中间开始显示——像「页面自己跳了一下」。归零让每次切入都从头看起。
  const main = document.querySelector(".app-main");
  if (main) main.scrollTop = 0;
  if (name === "rules") loadRules();
  if (name === "settings") loadSettings();
  if (name === "watch") refreshWatch();
  location.hash = name;
}

/* --------------------------------- 概览 --------------------------------- */

/* 引导区：先回答「现在该做什么」，再给数字。
   之前首屏是六个卡片 + 一段流程说明，用户看完仍不知道该点哪——
   数字是给已经懂流程的人看的，新用户需要的是一个动作。 */
/* 超时措辞与 CLI 的 timeout_phrase() 保持一致——同一件事在两处用两套说法，
   用户会以为是两回事。 */
function timeoutPhrase(meta) {
  const info = (meta && meta.timeout) || {};
  const bits = [info.phase ? `${info.phase}阶段` : "未知阶段"];
  if (info.seconds) bits.push(`超过 ${info.seconds} 秒`);
  if (info.processed !== undefined && info.processed !== null) {
    bits.push(`已处理 ${info.processed} 项`);
  }
  return bits.join("，");
}

/* 跨分页的「这次没跑完」警告。一份只扫了一半的产物，数字看起来和完整产物
   一模一样，所以必须摆在所有分页之上，不能只藏在概览里。 */
function renderPartialWarning(data) {
  const box = document.getElementById("partialWarn");
  const meta = (data && data.meta) || {};
  if (!meta.timed_out) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.textContent =
    `本次扫描超时（${timeoutPhrase(meta)}），只覆盖了一部分目录：` +
    "下面的数字不完整，本次也没有执行任何隔离。把扫描时限调大或设为 0 后重新扫描。";
}

function renderGuide(data) {
  const box = document.getElementById("overviewGuide");
  clear(box);
  const cand = (data.candidates || []).length;
  const pending = (data.pending_auto_hits || []).length;
  const summary = data.quarantine_summary || {};
  const qCount = summary.count || 0;
  const todo = cand + pending;

  let title;
  let sub;
  let actions;
  if ((data.meta || {}).timed_out) {
    // 超时的 run 绝不能走「这个目录很干净」那条分支——那是拿半棵树的结果下结论。
    title = "这次扫描没跑完";
    sub = `扫描超时（${timeoutPhrase(data.meta)}），下面的数字只覆盖了一部分目录，不能当成完整结论。`;
    actions = [["重新扫描", "scan"], ["去审阅", "review"]];
  } else if (!currentRun) {
    title = "还没有扫描结果";
    sub = "先扫描一次，看看这个目录里有什么可以清理。扫描只读元数据，不动任何文件。";
    actions = [["去扫描", "scan"]];
  } else if (todo) {
    title = `发现 ${todo} 项待处理`;
    sub = `规则命中 ${pending} 项（可直接执行），另有 ${cand} 项需要你逐条判断。`;
    actions = [["去审阅", "review"], ["重新扫描", "scan"]];
  } else if (qCount) {
    title = `隔离区有 ${qCount} 项，占用 ${humanSize(summary.size)}`;
    sub = "确认不需要了就彻底删除释放空间；发现删错了可以还原回原位。";
    actions = [["去清理隔离区", "quarantine"]];
  } else {
    title = "这个目录很干净";
    sub = "没有待处理项，隔离区也是空的。可以换个目录，或重新扫描看最新状态。";
    actions = [["重新扫描", "scan"]];
  }

  const textBox = el("div", { cls: "g-text" });
  textBox.appendChild(el("div", { cls: "g-title", text: title }));
  textBox.appendChild(el("div", { cls: "g-sub", text: sub }));
  box.appendChild(textBox);

  const act = el("div", { cls: "g-actions" });
  for (const [label, tab] of actions) {
    const b = el("button", { text: label, attrs: { type: "button" } });
    b.addEventListener("click", () => showTab(tab));
    act.appendChild(b);
  }
  box.appendChild(act);
  box.classList.toggle("done", !todo && !qCount);
}

/* 步骤条：四步一屏看清自己在哪一步；点击可跳到对应页 */
function renderSteps(data) {
  const box = document.getElementById("overviewSteps");
  clear(box);
  const cand = (data.candidates || []).length;
  const pending = (data.pending_auto_hits || []).length;
  const summary = data.quarantine_summary || {};
  const hasRun = !!currentRun;
  const todo = cand + pending;
  const qCount = summary.count || 0;

  const steps = [
    ["扫描", "scan", hasRun ? "done" : "now"],
    ["审阅", "review", !hasRun ? "todo" : todo ? "now" : "done"],
    ["执行", "execute", qCount ? "done" : "todo"],
    ["清理隔离区", "quarantine", qCount ? "now" : "todo"],
  ];
  steps.forEach(([label, tab, state], i) => {
    const s = el("div", { cls: "step", attrs: { "data-state": state, title: `去「${label}」` } });
    s.appendChild(el("span", { cls: "n", text: String(i + 1) }));
    s.appendChild(el("span", { text: label }));
    s.addEventListener("click", () => showTab(tab));
    box.appendChild(s);
  });
}

function renderOverview(data) {
  renderGuide(data);
  renderSteps(data);
  const box = document.getElementById("overviewCards");
  clear(box);
  const meta = data.meta || {};
  const summary = data.quarantine_summary || {};
  box.appendChild(card("候选 / 建议", (data.candidates || []).length, "待人工确认"));
  box.appendChild(card("待自动隔离", (data.pending_auto_hits || []).length, "规则命中，未执行"));
  box.appendChild(card("已隔离", summary.count || 0, "中间状态，可还原"));
  box.appendChild(card("隔离区占用", humanSize(summary.size), "确认后可彻底删除"));
  box.appendChild(card("已直接删除", summary.purged_directly || 0, "命中直删规则"));
  box.appendChild(card("受保护拦下", (data.protection || []).length, "本轮未动"));
  // run id 一并摆出来：命令行 `dirtidy apply-rules <root> <run_id>` 要的就是它。
  // 只在下拉里显示人话的话，用户想用命令行时又得去翻状态目录。
  document.getElementById("runHint").textContent = `run ${meta.run_id || "-"} · ${
    meta.applied ? "本轮已执行过自动隔离。" : "本轮尚未执行自动隔离。"
  }`;
}

/* --------------------------- 扫描页：本次结果 --------------------------- */

/* 扫描页原先只写一行「已生成 run」，扫完立刻跳到审阅页——用户在扫描页
   找不到任何结果，回头再看也只剩那行字。这里补上「这次扫出了什么」：
   一句结论 + 按规则分组的清单。

   分组键用规则的中文说明（同一条规则的命中归一组），不是 rule_id：
   用户想知道的是「哪一类东西占了地方」，不是内部编号。 */
function renderScanSummary(data) {
  const box = document.getElementById("scanResult");
  if (!box) return;
  clear(box);
  const meta = data.meta || {};
  const hits = data.pending_auto_hits || [];
  const cands = data.candidates || [];
  const protectedCount =
    Number(meta.protected_blocked || 0) + Number(meta.protected_blocked_by_descendant || 0);

  if (!meta.run_id) {
    box.appendChild(el("p", { cls: "hint", text: "还没有扫描结果。点上面的「开始扫描」。" }));
    return;
  }

  const byAdvice = { safe_clean: 0, clean: 0, review: 0 };
  for (const h of hits) {
    const k = ADVICE_KINDS.includes(h.advice) ? h.advice : "review";
    byAdvice[k] += 1;
  }
  const cleanable = byAdvice.safe_clean + byAdvice.clean;

  const bits = [];
  if (cleanable) {
    // 措辞与标签对齐：safe_clean 才叫「建议清理」，clean 只是「可以清理」。
    // 混着说会让用户以为 4 项都一样急。
    bits.push(`规则判定可清理 ${cleanable} 项（其中 ${byAdvice.safe_clean} 项重建成本为零）`);
  }
  if (cands.length) bits.push(`${cands.length} 项需要你判断`);
  if (protectedCount) bits.push(`${protectedCount} 项被保护拦下`);
  if (!bits.length) bits.push("没有发现可清理的项");
  const totalBytes = hits.reduce((s, h) => s + Math.max(0, Number(h.size_bytes) || 0), 0);
  if (totalBytes > 0) bits.push(`合计约 ${humanSize(totalBytes)}`);

  box.appendChild(el("p", { cls: "lead", text: `本次扫描：${bits.join("；")}。` }));
  box.appendChild(
    el("p", {
      cls: "hint",
      text: "扫描只读，没有移动或删除任何文件。下面按规则分组，展开可看具体是哪些。",
    }),
  );

  const groups = new Map();
  for (const h of hits) {
    const key = h.rule_id || "-";
    if (!groups.has(key)) groups.set(key, { rule_id: key, sample: h, paths: [], size: 0 });
    const g = groups.get(key);
    g.paths.push(h);
    g.size += Math.max(0, Number(h.size_bytes) || 0);
  }
  // 按占用降序：先看占地方最多的那一类。
  const groupList = [...groups.values()].sort((a, b) => b.size - a.size);
  for (const g of groupList) box.appendChild(scanGroup(g));
  if (!groupList.length) {
    box.appendChild(el("p", { cls: "empty-note", text: "本轮没有规则命中项。" }));
  }

  const bar = el("div", { cls: "toolbar" });
  const total = hits.length + cands.length;
  const go = el("button", {
    cls: "primary",
    text: total ? `去审阅页逐条确认（${total} 项）` : "去审阅页",
    attrs: { type: "button" },
  });
  go.onclick = () => showTab("review");
  bar.appendChild(go);
  box.appendChild(bar);
}

/* 一条规则一组：建议标签 + 中文说明 + 条数，具体文件收进折叠区。
   全摊开会把扫描页撑成几百行，而用户第一眼只需要知道「有几类、各多少」。 */
function scanGroup(g) {
  const wrap = el("div", { cls: "scan-group" });
  const head = el("div", { cls: "scan-group-head" });
  head.appendChild(adviceTag(g.sample));
  const title = el("div", { cls: "scan-group-title" });
  title.appendChild(
    el("strong", { text: g.sample.rule_reason || g.sample.reason || "（规则未给出说明）" }),
  );
  const metaBits = [`${g.paths.length} 项`];
  if (g.size > 0) metaBits.push(sizeText(g.size));
  metaBits.push(`规则：${ruleLabel(g.sample)}`);
  title.appendChild(el("span", { cls: "tiny", text: metaBits.join(" · ") }));
  head.appendChild(title);
  wrap.appendChild(head);

  const det = document.createElement("details");
  det.appendChild(el("summary", { text: `展开这 ${g.paths.length} 项` }));
  for (const h of g.paths) {
    const line = el("div", { cls: "rowline" });
    line.appendChild(el("code", { text: h.path }));
    if (h.is_dir) line.appendChild(el("span", { cls: "badge", text: "目录" }));
    line.appendChild(el("span", { cls: "tiny", text: sizeText(h.size_bytes) }));
    if (h.effect_label) line.appendChild(el("span", { cls: "tiny", text: h.effect_label }));
    line.appendChild(whyButton(h.path));
    det.appendChild(line);
  }
  wrap.appendChild(det);
  return wrap;
}

/* ------------------------------- 待自动隔离 ------------------------------- */

/* 待自动隔离：规则已经判定要搬走、但还没执行的项。
   这是「不误删」的终审入口——没有它，用户在网页上根本看不到规则打算动什么。

   列的顺序按「用户做决定要问的问题」排，不按数据结构排：
   路径 → 这是什么（中文说明）→ 建议怎么办 → 动了会怎样。
   规则 ID 不是给人看的，缩成小字放说明下面；原先它占着「命中规则」一整列，
   用户看到 `dep_node_modules` 只能猜。 */
/* 审阅页的筛选 / 排序 / 分组状态。放模块级而不是每次渲染重建：勾了「保留」
   会触发重渲染，这些选择必须跟着留住，否则用户筛完、排好、分完组，
   点一下又全回到默认，等于白做。 */
const pendingFilter = { advice: "", q: "" };
const candFilter = { q: "", rule: "", ai: "", decision: "" };
/* 默认按占用降序：用户第一眼要看的是「谁最占地方」，不是「谁字母序靠前」。
   Windows 磁盘清理、CleanMyMac、dupeGuru 都是这个排法。 */
const pendingSort = { key: "size", dir: -1 };
let pendingGrouped = false;

/* 每个排序键的默认方向：大小看「最大的在前」，路径看「A 在前」，
   建议看「越该清越前」。 */
const SORT_DEFAULT_DIR = { size: -1, path: 1, advice: 1 };

/* 未知档位一律当「需要你判断」——与 `adviceTag` 的兜底保持一致。
   不一致的话，分组视图会把这类行**直接漏掉**（分组键对不上任何一节）。 */
function adviceKind(h) {
  return ADVICE_KINDS.includes(h.advice) ? h.advice : "review";
}

function adviceRank(h) {
  return ADVICE_KINDS.indexOf(adviceKind(h));
}

function matchesPendingFilter(h) {
  // 用 adviceKind 兜底：未知档位在分组里算「需要你判断」，筛选时也得算进去，
  // 否则同一行会「分组里看得到、按那一档筛却筛不出来」。
  if (pendingFilter.advice && adviceKind(h) !== pendingFilter.advice) return false;
  if (pendingFilter.q && !String(h.path || "").toLowerCase().includes(pendingFilter.q)) {
    return false;
  }
  return true;
}

function sortHits(list) {
  const { key, dir } = pendingSort;
  const val = (h) => {
    if (key === "path") return String(h.path || "");
    if (key === "advice") return adviceRank(h);
    return Number(h.size_bytes) || 0;
  };
  return [...list].sort((a, b) => {
    const va = val(a);
    const vb = val(b);
    // 同值时按路径兜底：排序结果要稳定，否则每次重渲染顺序都在跳。
    if (va === vb) return String(a.path || "").localeCompare(String(b.path || ""));
    return (va < vb ? -1 : 1) * dir;
  });
}

/* 表头箭头：让用户知道「现在按什么排、朝哪个方向」，而不是靠猜。 */
function markSortHeaders() {
  for (const th of document.querySelectorAll("#tab-review th.sortable")) {
    const on = th.dataset.sort === pendingSort.key;
    th.dataset.dir = on ? (pendingSort.dir === 1 ? "asc" : "desc") : "";
    const name = th.textContent.trim();
    th.title = on
      ? `当前按「${name}」${pendingSort.dir === 1 ? "升序" : "降序"}，点一下切换`
      : `点一下按「${name}」排序`;
  }
}

function renderPending(data) {
  const tbody = document.getElementById("pendingRows");
  clear(tbody);
  const all = sortHits(data.pending_auto_hits || []);
  const hits = all.filter(matchesPendingFilter);
  if (!all.length) {
    emptyRow(tbody, 6, "本轮没有规则命中项。切到「扫描」页跑一次，或换一个 run。");
  } else if (!hits.length) {
    // 有命中但被筛掉了：必须说清是「筛没了」而不是「本来就没有」，
    // 否则用户会以为扫描结果丢了。
    emptyRow(tbody, 6, "当前筛选没有匹配项。把「只看」改回「全部」，或清空搜索框。");
  }
  // 命中目录会覆盖其内的文件命中（执行时只搬父目录）。这里把它标出来——
  // 否则页面写着「N 项」而实际只搬走更少，用户按数字核对会以为漏了。
  // 用全量而不是筛后结果：筛掉父目录不该让子项的标记凭空消失。
  const dirPaths = new Set(all.filter((h) => h.is_dir).map((h) => `${h.path}/`));
  const coveredByParent = (p) => {
    let slash = p.lastIndexOf("/");
    while (slash >= 0) {
      if (dirPaths.has(p.slice(0, slash + 1))) return true;
      if (slash === 0) break;
      slash = p.lastIndexOf("/", slash - 1);
    }
    return false;
  };
  const pageCount = Math.max(1, Math.ceil(hits.length / REVIEW_PAGE_SIZE));
  pendingPage = Math.min(pendingPage, pageCount);
  const pageHits = hits.slice((pendingPage - 1) * REVIEW_PAGE_SIZE, pendingPage * REVIEW_PAGE_SIZE);
  document.getElementById("pendingPageInfo").textContent = `第 ${pendingPage} / ${pageCount} 页`;
  document.getElementById("pendingPrev").disabled = pendingPage <= 1;
  document.getElementById("pendingNextPage").disabled = pendingPage >= pageCount;

  if (pendingGrouped && pageHits.length) {
    // 分节显示：三档各一段。并排三栏会把每栏压到 500px 左右、路径都显示不全，
    // 分节横条既不牺牲列宽，又能一次看清「哪档有几项、共占多少」。
    for (const kind of ADVICE_KINDS) {
      const group = pageHits.filter((h) => adviceKind(h) === kind);
      if (!group.length) continue;
      tbody.appendChild(pendingGroupRow(group));
      for (const h of group) tbody.appendChild(pendingRow(h, coveredByParent));
    }
  } else {
    for (const h of pageHits) tbody.appendChild(pendingRow(h, coveredByParent));
  }

  markSortHeaders();

  // 执行范围始终是**全部命中**（勾「保留」的除外），与筛选、排序、分组都无关。
  // 但「几项命中」不等于「几项会被搬走」——目录覆盖后代、子树含保护路径都会让
  // 实际条数更少。那个数字只能由后端 dry-run 给（`refreshPlan`），这里只负责
  // 按「保留」勾选的变化去重新核对，不自己算。
  const keptCount = all.filter((h) => keptPaths.has(h.path)).length;
  const allBox = document.getElementById("pendingAll");
  if (allBox) {
    allBox.checked = all.length > 0 && keptCount === all.length;
    allBox.indeterminate = keptCount > 0 && keptCount < all.length;
  }
  const cnt = document.getElementById("pendingCount");
  if (cnt) {
    cnt.textContent = all.length
      ? `本页 ${pageHits.length} 项 / 筛选匹配 ${hits.length} 项 / 共 ${all.length} 项（筛选只影响显示，分页也不改变执行范围；执行仍按全部命中处理）`
      : "";
  }
  // 「保留」勾选会影响执行页的清单与数字，改完就地重画那一页，别等用户切过去才发现是旧的。
  renderExecute(currentRunData);
  // 勾选变了 → 执行范围也变了 → 重新向后端要一份预览。防抖，避免连点几次发一串请求。
  schedulePlanRefresh();
}

/* 分组标题行：一条占满整行的横条，回答「这一档是什么、多少项、共占多少」。 */
function pendingGroupRow(group) {
  const tr = document.createElement("tr");
  tr.className = "group-row";
  const td = document.createElement("td");
  td.setAttribute("colspan", "6");
  td.appendChild(adviceTag(group[0]));
  const size = group.reduce((s, h) => s + Math.max(0, Number(h.size_bytes) || 0), 0);
  const bits = [`本页 ${group.length} 项`];
  if (size > 0) bits.push(`合计 ${humanSize(size)}`);
  td.appendChild(el("span", { cls: "group-meta", text: bits.join(" · ") }));
  tr.appendChild(td);
  return tr;
}

/* 一行命中项。抽成函数是为了排序 / 筛选 / 分组三种视图共用同一份构造逻辑——
   三份各写一遍的话，改一列就得改三处，迟早漏一处。 */
function pendingRow(h, coveredByParent) {
  const tr = document.createElement("tr");

  const tdKeep = document.createElement("td");
  const cb = el("input", { attrs: { type: "checkbox", "data-path": h.path } });
  cb.checked = keptPaths.has(h.path);
  cb.addEventListener("change", () => {
    if (cb.checked) keptPaths.add(h.path);
    else keptPaths.delete(h.path);
    renderPending(currentRunData);
  });
  tdKeep.appendChild(cb);
  tr.appendChild(tdKeep);

  const tdPath = document.createElement("td");
  tdPath.appendChild(el("code", { text: h.path }));
  tdPath.appendChild(whyButton(h.path));
  if (h.is_dir) tdPath.appendChild(el("span", { cls: "badge", text: "目录" }));
  if (coveredByParent(h.path)) {
    tdPath.appendChild(
      el("span", {
        cls: "badge",
        text: "已含在上层目录里",
        attrs: { title: "执行时只搬父目录，这一条不会单独搬走" },
      }),
    );
  }
  tr.appendChild(tdPath);

  // 这是什么：中文说明为主，规则 ID 退成小字（要排查时还在，不占视线）
  const tdWhat = document.createElement("td");
  tdWhat.appendChild(el("div", { text: h.rule_reason || h.reason || "（规则未给出说明）" }));
  if (h.rule_id) {
    tdWhat.appendChild(
      el("div", {
        cls: "tiny",
        text: `规则：${ruleLabel(h)}`,
        attrs: { title: h.rule_id },
      }),
    );
  }
  tr.appendChild(tdWhat);

  const tdAdvice = document.createElement("td");
  tdAdvice.appendChild(adviceTag(h));
  // 「删了怎么恢复」比形容词更能让人下决心：说清代价，而不是给个「安全」标签
  if (h.recover) {
    tdAdvice.appendChild(el("div", { cls: "tiny", text: `删了怎么办：${h.recover}` }));
  }
  tr.appendChild(tdAdvice);

  tr.appendChild(el("td", { cls: "num", text: sizeText(h.size_bytes) }));

  // 动了会怎样：可还原 / 不可还原，比「是 / 否」有用得多
  tr.appendChild(el("td", { text: h.effect_label || "搬进隔离区（可还原）" }));
  return tr;
}

function renderProtection(data) {
  const box = document.getElementById("protection");
  clear(box);
  const records = data.protection || [];
  const policy = data.protect_policy || [];

  if (!records.length) {
    box.appendChild(el("div", { text: "本轮没有被保护拦下的项。" }));
  }
  for (const r of records) {
    const line = el("div", { cls: "rowline" });
    line.appendChild(el("code", { text: r.path }));
    line.appendChild(
      el("span", {
        text: ` ← ${r.protect_reason || "受保护，不会自动清理"}`,
        attrs: { title: r.protect_rule_id || "" },
      }),
    );
    if (r.intended_rule_id) {
      line.appendChild(el("span", {
        text: "；已拦下原定清理动作",
        attrs: { title: r.intended_rule_id },
      }));
    }
    line.appendChild(whyButton(r.path));
    box.appendChild(line);
  }
  if (policy.length) {
    // 11 条内部规则 ID 直接铺在页面上没人看得懂，还把这一屏撑得很长。
    // 收进折叠区：想知道细节的点开，不需要的不被它干扰。
    const det = document.createElement("details");
    det.appendChild(
      el("summary", { text: `生效保护策略 ${policy.length} 条（点开看清单）` }),
    );
    for (const p of policy) {
      det.appendChild(el("div", {
        cls: "hint",
        text: p.reason || "受保护，不会自动清理",
        attrs: { title: p.id || "" },
      }));
    }
    box.appendChild(det);
  }
}

/* --------------------------------- 隔离区 --------------------------------- */

function renderQuarantine(data) {
  const summary = data.quarantine_summary || {};
  const cards = document.getElementById("quarantineCards");
  clear(cards);
  cards.appendChild(card("条目数", summary.count || 0));
  cards.appendChild(card("占用空间", humanSize(summary.size), "释放空间就靠这一页"));
  cards.appendChild(card("已直接删除", summary.purged_directly || 0, "不可还原"));

  const tbody = document.getElementById("quarantineRows");
  clear(tbody);
  const entries = data.quarantine_manifest || [];
  const liveIds = new Set(entries.map((e) => e.id));
  for (const id of [...quarantined]) if (!liveIds.has(id)) quarantined.delete(id);

  if (!entries.length) {
    emptyRow(
      tbody,
      7,
      "隔离区是空的。「审阅」页执行自动隔离、或对候选选「隔离」并执行后，条目会出现在这里。",
    );
  }

  for (const e of entries) {
    const tr = document.createElement("tr");

    const tdSel = document.createElement("td");
    const cb = el("input", { attrs: { type: "checkbox", "data-id": e.id } });
    cb.checked = quarantined.has(e.id);
    cb.addEventListener("change", () => {
      if (cb.checked) quarantined.add(e.id);
      else quarantined.delete(e.id);
    });
    tdSel.appendChild(cb);
    tr.appendChild(tdSel);

    const tdPath = document.createElement("td");
    tdPath.appendChild(el("code", { text: e.original_path }));
    tdPath.appendChild(whyButton(e.original_path));
    tr.appendChild(tdPath);

    // 原因在前、规则名在后：用户要判断「这条要不要还原」，先看的是「为什么被搬走」，
    // 规则编号只在排查时才有用（所以显示中文短名，id 挂在 title 上）。
    tr.appendChild(el("td", { text: e.reason || "-" }));
    tr.appendChild(
      el("td", {
        cls: "tiny",
        text: ruleLabel(e) || "-",
        attrs: e.rule_id ? { title: e.rule_id } : {},
      }),
    );
    tr.appendChild(el("td", { text: e.is_dir ? "目录" : "文件" }));
    // 用后端实算的 size_actual：清单里的 size 对目录恒为 0，
    // 逐行显示 0 B 而合计显示 293 KB 会自相矛盾。
    tr.appendChild(
      el("td", { text: e.exists === false ? "已不在磁盘" : humanSize(e.size_actual) }),
    );
    tr.appendChild(el("td", { text: e.quarantined_at || "" }));
    tbody.appendChild(tr);
  }

  const restoreSel = document.getElementById("restoreSelBtn");
  const purgeSel = document.getElementById("purgeSelBtn");
  restoreSel.disabled = quarantined.size === 0;
  purgeSel.disabled = quarantined.size === 0;
  restoreSel.textContent = `还原选中（${quarantined.size}）`;
  purgeSel.textContent = `彻底删除选中（${quarantined.size}）`;
  document.getElementById("restoreAllBtn").disabled = entries.length === 0;
  document.getElementById("purgeAllBtn").disabled = entries.length === 0;

  const purgedBox = document.getElementById("purgedBox");
  clear(purgedBox);
  const purged = data.purged_directly || [];
  if (!purged.length) {
    purgedBox.appendChild(el("div", { text: "本次 run 还没有被直接删除的条目。" }));
    return;
  }
  for (const p of purged) {
    const line = el("div", { cls: "rowline" });
    line.appendChild(el("code", { text: p.original_path }));
    line.appendChild(
      document.createTextNode(
        ` ← 规则 ${ruleLabel(p) || "-"}，于 ${p.quarantined_at || ""} 删除`,
      ),
    );
    purgedBox.appendChild(line);
  }
}

/* --------------------------------- 审阅 --------------------------------- */

function renderAi(data) {
  const box = document.getElementById("aiBox");
  const hint = document.getElementById("aiHint");
  clear(box);
  const suggestions = data.ai_suggestions || [];
  const meta = data.meta || {};
  if (!suggestions.length) {
    hint.textContent =
      "未运行模型分析。模型是可选增强：规则处理完仍有遗留项时再跑，只分析还没表态的候选。";
    return;
  }
  hint.textContent = `本次共 ${suggestions.length} 条建议（只分析规则处理完剩下的遗留项）。模型输出只是参考，最终仍以你的决策为准。`;
  const table = el("table");
  const thead = el("thead");
  const hr = el("tr");
  for (const h of ["路径", "类型", "价值", "建议动作", "置信度", "说明"]) {
    hr.appendChild(el("th", { text: h }));
  }
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = el("tbody");
  for (const s of suggestions) {
    const tr = el("tr");
    tr.appendChild(el("td", { text: s.path || "" }));
    tr.appendChild(el("td", { text: s.asset_type || "" }));
    tr.appendChild(el("td", { text: s.value || "" }));
    tr.appendChild(el("td", { text: s.action || "" }));
    tr.appendChild(el("td", { text: s.confidence == null ? "" : String(s.confidence) }));
    tr.appendChild(el("td", { text: s.rationale || "" }));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  box.appendChild(table);
  if (meta.applied) box.appendChild(el("div", { cls: "hint", text: "注意：本轮已执行过隔离。" }));
}

function renderReview(data) {
  renderPending(data);
  renderProtection(data);
  renderAi(data);
  renderCandidates(data);
  renderExecute(data);
}

/* 同一条路径可能有多条候选：一个压缩包既可能被规则判为「可能存着唯一副本」，
   又可能被内容比对判为「已解压」。原先用 path → 单条候选，后写的会把先写的
   盖掉，风险提示就此消失。这里保留成数组，全部展示。 */
function candsByPathOf(data) {
  const map = new Map();
  for (const c of data.candidates || []) {
    if (c.kind === "duplicate_group") continue;
    for (const p of c.paths || []) {
      if (!map.has(p)) map.set(p, []);
      map.get(p).push(c);
    }
  }
  return map;
}

/* 多条候选 → 最保守的那一档。这里只按后端给的档位顺序挑选，
   不在前端重新判定；挑不出来就交人工。 */
function mostCautious(cands) {
  let best = null;
  for (const c of cands) {
    if (!ADVICE_KINDS.includes(c.advice)) continue;
    if (!best || ADVICE_KINDS.indexOf(c.advice) > ADVICE_KINDS.indexOf(best.advice)) best = c;
  }
  return best || { advice: "review" };
}

/* 候选表。翻页、勾选、改决策只重画这张表和执行页，不重建待自动隔离表，
   也不重新请求规则 dry-run（那会让执行按钮无谓地回到「等待预览核对」）。 */
function renderCandidates(data) {
  const rows = document.getElementById("rows");
  clear(rows);
  const suggestions = {};
  for (const s of data.ai_suggestions || []) suggestions[s.path] = s;
  currentSuggestions = suggestions;

  const candByPath = candsByPathOf(data);
  const paths = new Set(candByPath.keys());
  for (const p of Object.keys(suggestions)) paths.add(p);

  const allPaths = [...paths];
  const ruleSelect = document.getElementById("candRule");
  const ruleOptions = new Map();
  for (const cands of candByPath.values()) {
    for (const cand of cands) {
      if (cand.rule_id) ruleOptions.set(cand.rule_id, ruleLabel(cand));
    }
  }
  clear(ruleSelect);
  ruleSelect.appendChild(el("option", { text: "全部", attrs: { value: "" } }));
  for (const [id, label] of [...ruleOptions].sort((a, b) => a[1].localeCompare(b[1]))) {
    ruleSelect.appendChild(el("option", { text: label, attrs: { value: id } }));
  }
  if (candFilter.rule && !ruleOptions.has(candFilter.rule)) candFilter.rule = "";
  ruleSelect.value = candFilter.rule;
  const shownPaths = allPaths.filter((p) => {
    const list = candByPath.get(p) || [];
    const ai = suggestions[p] ? suggestions[p].action : "none";
    return (!candFilter.q || p.toLowerCase().includes(candFilter.q)) &&
      (!candFilter.rule || list.some((c) => c.rule_id === candFilter.rule)) &&
      (!candFilter.ai || ai === candFilter.ai) &&
      (!candFilter.decision || decisionFor(p).action === candFilter.decision);
  });
  currentCandMatching = shownPaths;
  if (!allPaths.length) {
    emptyRow(rows, 7, "本轮没有候选文件。用完整扫描（不勾「跳过内容 hash」）才会产出候选与重复组。");
  } else if (!shownPaths.length) {
    emptyRow(rows, 7, "当前筛选没有匹配的候选。把筛选条件清掉看全部。");
  }
  // 同一路径的多条候选共用同一份文件，取其中最大的已算值即可（同样的答案，不重复相加）。
  const sizeOf = (p) => {
    let n = 0;
    for (const c of candByPath.get(p) || []) n = Math.max(n, Number(c.size_bytes) || 0);
    return n;
  };
  // 按占用降序，与「待自动隔离」表一致：先看大的。
  const sortedPaths = shownPaths.sort((a, b) => sizeOf(b) - sizeOf(a));
  const pageCount = Math.max(1, Math.ceil(sortedPaths.length / REVIEW_PAGE_SIZE));
  candPage = Math.min(candPage, pageCount);
  const pagePaths = sortedPaths.slice((candPage - 1) * REVIEW_PAGE_SIZE, candPage * REVIEW_PAGE_SIZE);
  document.getElementById("candPageInfo").textContent = `第 ${candPage} / ${pageCount} 页`;
  document.getElementById("candPrev").disabled = candPage <= 1;
  document.getElementById("candNextPage").disabled = candPage >= pageCount;
  for (const path of pagePaths) {
    const sug = suggestions[path];
    const cands = candByPath.get(path) || [];
    // 唯一真相是 decisions（默认 keep）。这里**刻意不按建议预填**：
    // 预填只写 DOM、不进 decisions，用户看着预填内容点「执行」会以为已采纳，
    // 实际什么都不发生。要采纳就点「采纳全部建议」，那是显式的。
    const d = decisionFor(path);
    const tr = document.createElement("tr");

    // 批量选择：勾选只进 candPicked，不改决策、不碰文件。
    // 勾完再选一个动作点「应用到选中的」，才是写 decisions。
    const tdPick = document.createElement("td");
    const pick = el("input", {
      cls: "cand-pick",
      attrs: { type: "checkbox", "data-path": path },
    });
    pick.checked = candPicked.has(path);
    pick.addEventListener("change", () => {
      if (pick.checked) candPicked.add(path);
      else candPicked.delete(path);
      refreshCandPicked(currentCandShown);
    });
    tdPick.appendChild(pick);
    tr.appendChild(tdPick);

    const tdPath = document.createElement("td");
    tdPath.appendChild(el("code", { text: path }));
    tdPath.appendChild(whyButton(path));
    tr.appendChild(tdPath);

    // 为什么在列表里：每条候选各给一行原因，不合并、不覆盖——
    // 同一个压缩包可能既有「可能存着唯一副本」又有「内容已解压」，两条都得看见。
    const tdWhy = document.createElement("td");
    tdWhy.appendChild(
      el("span", {
        cls: sug ? "badge badge-ai" : "badge",
        text: sug ? "模型建议" : "规则候选",
      }),
    );
    for (const c of cands) {
      const why = c.rule_reason || c.reason || "";
      if (why) tdWhy.appendChild(el("div", { cls: "tiny", text: why }));
      if (c.rule_id) {
        tdWhy.appendChild(
          el("div", {
            cls: "tiny",
            text: `规则：${ruleLabel(c)}`,
            attrs: { title: c.rule_id },
          }),
        );
      }
    }
    tr.appendChild(tdWhy);

    // 建议：规则给的是「需要你判断」，模型给了就一并摆出来（仍然只是参考）
    const tdAdvice = document.createElement("td");
    tdAdvice.appendChild(adviceTag(mostCautious(cands)));
    const recovered = new Set();
    for (const c of cands) {
      const text = String(c.recover || "").trim();
      if (!text || recovered.has(text)) continue;
      recovered.add(text);
      tdAdvice.appendChild(el("div", { cls: "tiny", text: `删了怎么办：${text}` }));
    }
    if (sug) {
      const conf = Number(sug.confidence);
      const pct = Number.isFinite(conf) ? `（置信度 ${Math.round(conf > 1 ? conf : conf * 100)}%）` : "";
      tdAdvice.appendChild(
        el("div", {
          cls: "tiny",
          text: `模型：${DECISION_LABELS[sug.action] || sug.action}${pct}`,
        }),
      );
    }
    tr.appendChild(tdAdvice);

    tr.appendChild(el("td", { cls: "num", text: sizeText(cands.length ? sizeOf(path) : -1) }));

    const tdAct = document.createElement("td");
    const sel = el("select", { cls: "act", attrs: { "data-path": path } });
    for (const v of ["keep", "quarantine", "archive"]) {
      sel.appendChild(el("option", { text: DECISION_LABELS[v], attrs: { value: v } }));
    }
    sel.value = d.action;
    tdAct.appendChild(sel);
    tr.appendChild(tdAct);

    const tdTgt = document.createElement("td");
    const inp = el("input", {
      cls: "tgt",
      attrs: { "data-path": path, placeholder: "archive/..." },
    });
    inp.value = d.target_relpath || "";
    tdTgt.appendChild(inp);
    tr.appendChild(tdTgt);
    // 目标路径只有「归档」才用得上；其余动作下留着内容只会误导
    // （出现过「决策=保留 而 归档路径已填」的矛盾观感）。
    syncTargetEnabled(tr);

    rows.appendChild(tr);
  }
  currentCandShown = pagePaths;
  refreshCandPicked(pagePaths);

  // 重复组：单选保留项，其余可一键标 quarantine
  const dups = document.getElementById("dups");
  clear(dups);
  const dupGroups = (data.candidates || []).filter((c) => c.kind === "duplicate_group");
  if (!dupGroups.length) {
    dups.appendChild(
      el("div", {
        cls: "empty-note",
        text: "本轮没有重复组。重复组需要算内容 hash——「扫描」页不勾「跳过内容 hash」才会产出。",
      }),
    );
  }
  for (const c of dupGroups) dups.appendChild(dupGroupBox(c));

  rows.querySelectorAll(".act").forEach((node) => {
    node.addEventListener("change", () => {
      const path = node.dataset.path;
      const cur = decisionFor(path);
      cur.action = node.value;
      decisions.set(path, cur);
      syncTargetEnabled(node.closest("tr"));
      refreshDecisionViews(currentRunData);
    });
  });
  rows.querySelectorAll(".tgt").forEach((node) => {
    node.addEventListener("change", () => {
      const path = node.dataset.path;
      const cur = decisionFor(path);
      cur.target_relpath = node.value;
      decisions.set(path, cur);
      refreshDecisionViews(currentRunData);
    });
  });

  refreshAdoptHint();
  const candCount = document.getElementById("candCount");
  if (candCount) {
    candCount.textContent = allPaths.length
      ? `本页 ${pagePaths.length} 项 / 筛选匹配 ${shownPaths.length} 项 / 共 ${allPaths.length} 项`
      : "";
  }
  // 决策改了、或者采纳了模型建议，执行页的数字和清单都要重画
  refreshDecisionViews(currentRunData);
}

/* 本 run 里参与人工决策的全部路径：普通候选 + 重复组 + 只有模型建议的路径。
   执行清单和「已有 N 项」都必须按这个并集算——只统计普通候选时，
   「重复组已标隔离」会显示成 0 项，执行按钮还是禁用的。 */
function reviewedPaths(data) {
  const out = new Set();
  for (const c of (data && data.candidates) || []) {
    for (const p of c.paths || []) out.add(p);
  }
  for (const p of Object.keys(currentSuggestions || {})) out.add(p);
  return out;
}

function movingDecisions(data) {
  const scope = reviewedPaths(data);
  const moving = [];
  for (const [path, d] of decisions) {
    if (!scope.has(path) || d.action === "keep") continue;
    moving.push({ path, action: d.action, target: d.target_relpath || "" });
  }
  return moving;
}

/* 决策变了：执行页清单 / 数字 / 按钮状态一起重画，不等用户切过去才发现是旧的。 */
function refreshDecisionViews(data) {
  renderExecute(data);
  const candNext = document.getElementById("candNext");
  if (!candNext) return;
  const n = movingDecisions(data).length;
  candNext.textContent = n
    ? `已有 ${n} 项设为隔离 / 归档，去「执行」页才会真正搬走。`
    : "每行默认「保留不动」；改完决策去「执行」页动手。";
}

/* --------------------------- 执行页：动手 --------------------------- */

/* 这一页是步骤条上一直写着、但之前并不存在的那一步。
   它只做一件事：把「将要发生什么」摆清楚，然后让用户按下去。
   两个按钮各自紧挨自己那份清单——原先两个执行按钮分散在两张表下面，
   用户在候选表设完下拉，视线内最近的却是「执行自动隔离」（另一条路径）。 */
let planTimer = 0;
let planVersion = 0;

function planSummary(plan) {
  if (!plan) return "尚未预览";
  const refused = (plan.purge_refused || []).length;
  return `本轮将搬走 ${plan.would_quarantine || 0} 项，其中 ${plan.would_purge || 0} 项将直接删除（不可还原）；人工保留 ${plan.kept_by_user || 0} 项，保护拦下 ${plan.blocked_by_protect || 0} 项。${refused ? ` ${refused} 条规则未获准直删，只隔离不删除。` : ""}`;
}

function schedulePlanRefresh() {
  currentPlan = null;
  window.clearTimeout(planTimer);
  window.setTimeout(() => renderExecute(currentRunData), 0);
  planTimer = window.setTimeout(() => refreshPlan().catch((e) => setStatus(e, { error: true })), 120);
}

async function refreshPlan() {
  if (!currentRun) return null;
  const runId = currentRun;
  const version = ++planVersion;
  const keep_paths = [...keptPaths];
  currentPlan = null;
  renderExecute(currentRunData);
  const plan = await api(`/api/runs/${runId}/apply-rules`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keep_paths, dry_run: true }),
  });
  if (runId !== currentRun || version !== planVersion || JSON.stringify(keep_paths) !== JSON.stringify([...keptPaths])) return null;
  currentPlan = plan;
  renderExecute(currentRunData);
  return plan;
}

function renderExecute(data) {
  if (!data) return;
  const hits = data.pending_auto_hits || [];
  const kept = hits.filter((h) => keptPaths.has(h.path));
  const plan = currentPlan;
  const willMove = plan ? Number(plan.would_quarantine || 0) : null;

  const moving = movingDecisions(data);
  const q = moving.filter((m) => m.action === "quarantine").length;
  const arch = moving.length - q;

  const box = document.getElementById("execCards");
  if (box) {
    clear(box);
    box.appendChild(card("将搬走（规则命中）", willMove ?? "待核对", plan ? `保护拦下 ${plan.blocked_by_protect || 0} 项；直删 ${plan.would_purge || 0} 项` : "正在向后端核对"));
    box.appendChild(card("拟隔离（候选）", q, "人工选择，实际范围以执行前预览为准"));
    box.appendChild(card("拟归档（候选）", arch, "人工选择，实际范围以执行前预览为准"));
  }

  // 规则命中清单
  const rl = document.getElementById("execRulesList");
  if (rl) {
    clear(rl);
    if (!hits.length) {
      rl.appendChild(el("div", { cls: "empty-note", text: "本轮没有规则命中项。" }));
    } else if (willMove === 0) {
      rl.appendChild(el("div", { cls: "empty-note", text: "后端预览确认没有可搬走项（人工保留或保护拦下）。" }));
    } else {
      for (const h of hits.slice(0, 30)) {
        const line = el("div", { cls: keptPaths.has(h.path) ? "execrow kept" : "execrow" });
        line.appendChild(el("code", { text: h.path }));
        line.appendChild(el("span", { cls: "exec-meta", text: ruleLabel(h) }));
        line.appendChild(el("span", { cls: "exec-size", text: sizeText(h.size_bytes) }));
        const disposition = keptPaths.has(h.path) ? "人工保留"
          : !plan ? "待核对"
          : (plan.blocked_paths || []).includes(h.path) ? "保护拦下"
          : (plan.would_purge_paths || []).includes(h.path) ? "直删（不可还原）"
          : (plan.paths || []).includes(h.path) ? "将隔离"
          : (plan.kept_paths || []).includes(h.path) ? "人工保留"
          : "不单独执行";
        line.appendChild(el("span", { cls: "exec-tag", text: disposition }));
        rl.appendChild(line);
      }
      if (hits.length > 30) {
        rl.appendChild(el("div", { cls: "tiny", text: `……还有 ${hits.length - 30} 项未列出` }));
      }
    }
  }

  const rh = document.getElementById("execRulesHint");
  if (rh) {
    rh.textContent = plan ? planSummary(plan) : "预览尚未完成；执行按钮暂不可用。";
  }
  const next = document.getElementById("pendingNext");
  if (next) {
    next.textContent = plan ? planSummary(plan) : "正在核对实际可执行范围……";
  }

  // 候选决策清单
  const cl = document.getElementById("execCandList");
  if (cl) {
    clear(cl);
    if (!moving.length) {
      cl.appendChild(el("div", { cls: "empty-note", text: "还没有要执行的决策：每行默认「保留不动」，去「审阅」页改「决策」列或用批量选择。" }));
    } else {
      for (const m of moving.slice(0, 30)) {
        const line = el("div", { cls: "execrow" });
        line.appendChild(el("code", { text: m.path }));
        line.appendChild(el("span", { cls: "exec-meta", text: DECISION_LABELS[m.action] || m.action }));
        if (m.action === "archive" && m.target) line.appendChild(el("span", { cls: "exec-meta", text: `→ ${m.target}` }));
        cl.appendChild(line);
      }
      if (moving.length > 30) {
        cl.appendChild(el("div", { cls: "tiny", text: `……还有 ${moving.length - 30} 项未列出` }));
      }
    }
  }

  const ch = document.getElementById("execCandHint");
  if (ch) {
    ch.textContent = moving.length
      ? `按「决策」列，拟处理 ${moving.length} 项（隔离 ${q}、归档 ${arch}）；实际可执行项需由后端预览核对。`
      : "按「决策」列，将搬走 0 项。";
  }

  // 按钮
  const rb = document.getElementById("applyRulesBtn");
  if (rb) {
    rb.disabled = willMove === null || willMove === 0;
    rb.textContent = willMove === null ? "等待预览核对" : willMove ? `执行自动隔离（${willMove} 项）` : "无可执行项";
  }
  const ab = document.getElementById("applyBtn");
  if (ab) {
    ab.disabled = moving.length === 0;
    ab.textContent = moving.length ? `执行候选决策（${moving.length} 项）` : "没有待执行的决策";
  }
}

/* 重复组：同一份内容散落在好几处，留哪份只能人来定。
   标题直接把账报出来——「几份 · 每份多大 · 保留一份能释放多少」。
   原先是「相同内容 hash，需人工选留」一句话，账全留给用户自己算，
   而这个决定恰恰就是「省下多少空间」换「删错哪一份」的权衡。 */
function dupGroupBox(c) {
  const paths = Array.isArray(c.paths) ? c.paths.map(String) : [];
  const sizes = c.path_sizes || {};
  const info = c.meta || {};
  const total = Number(info.count) || paths.length;
  const truncated = Boolean(info.truncated);
  const box = el("div", { cls: "dup-group" });

  // 同内容 hash 的各份字节数必然相同，取第一份算出来即可。
  const known = paths.filter((p) => Number.isFinite(sizes[p]));
  const each = known.length ? sizes[known[0]] : NaN;
  const bits = [`${total} 份`];
  if (Number.isFinite(each) && each > 0) bits.push(`每份 ${humanSize(each)}`);
  // 「可释放」只在列表完整、且每份大小都算得出来时才给：截断的组少算了几份，
  // 报出来的数会偏小，那还不如不报——数字错了比没有数字更糟。
  if (
    Number.isFinite(each) &&
    each > 0 &&
    !truncated &&
    known.length === paths.length &&
    paths.length >= 2
  ) {
    bits.push(`保留一份可释放 ${humanSize(each * (paths.length - 1))}`);
  }
  box.appendChild(el("div", { cls: "dup-head", text: bits.join(" · ") }));
  box.appendChild(
    el("div", {
      cls: "tiny",
      text: c.rule_reason || c.reason || "内容完全相同，保留任意一份即可",
    }),
  );

  const groupName = `dup-${info.sha256 || paths[0] || ""}`;
  // 只有「恰好一份被明确标为保留」时才回填单选：新建 run 里所有路径的默认决策
  // 都是 keep，全勾等于没勾（radio 组只会留下最后一个，看起来像随便挑了一份）。
  const kept = paths.filter((p) => {
    const d = decisions.get(p);
    return d && d.action === "keep";
  });
  const radios = [];
  for (const p of paths) {
    const d = decisions.get(p);
    const radio = el("input", { attrs: { type: "radio", name: groupName, value: p } });
    radio.checked = kept.length === 1 && kept[0] === p;
    radios.push(radio);

    const line = el("div", { cls: "dup-row" });
    const label = document.createElement("label");
    label.appendChild(radio);
    label.appendChild(document.createTextNode(" 保留 "));
    label.appendChild(el("code", { text: p }));
    line.appendChild(label);
    line.appendChild(el("span", { cls: "dup-size", text: sizeText(sizes[p]) }));
    line.appendChild(el("span", { cls: "dup-state", text: dupStateText(d) }));
    box.appendChild(line);
  }

  const btn = el("button", { text: "其余标记隔离", attrs: { type: "button" } });
  btn.onclick = () => {
    const picked = radios.find((r) => r.checked);
    if (!picked) return alert("请先选一份要保留的");
    for (const p of paths) {
      decisions.set(p, {
        path: p,
        action: p === picked.value ? "keep" : "quarantine",
        target_relpath: "",
        note: "duplicate pick",
      });
    }
    setStatus({ updated_duplicates: paths });
    // 只重画候选表与执行页。原先这里调 loadRun，会把用户**尚未保存**的决策
    // 连同刚写进去的重复组选择一起清掉——服务端还没收到这些改动。
    renderCandidates(currentRunData);
  };
  box.appendChild(btn);
  return box;
}

/* 组内每份的当前状态。没进过 decisions 的行是「未表态」而不是「保留」——
   默认决策是保留，但把它显示成「保留」会让人以为这组已经选好了。 */
function dupStateText(d) {
  if (!d) return "未表态";
  if (d.action === "keep") return "保留";
  return `待${DECISION_LABELS[d.action] || d.action}`;
}

/* 归档目标路径只在决策 = 「归档」时可用。其余动作下留着内容会误导——
   截图里就出现过「决策 = 保留不动 而 归档路径已填」的矛盾观感。 */
function syncTargetEnabled(tr) {
  if (!tr) return;
  const sel = tr.querySelector("select.act");
  const inp = tr.querySelector("input.tgt");
  if (!sel || !inp) return;
  const need = sel.value === "archive";
  inp.disabled = !need;
  inp.placeholder = need ? "archive/..." : "（仅「归档」需要）";
}

/* 可采纳的建议 = 模型**明确**说了隔离 / 归档、**且用户还没表态**的行。
   - `keep` 与 `review` 不动：`review` 是模型自己说「拿不准」，变成决策等于替用户做主。
   - 已进过 `decisions` 的行跳过：采纳是「帮你把没表态的填上」，
     不是「覆盖你已经改过的」——否则用户手动把某行改成保留，再点一次采纳又被翻回去。 */
function adoptableSuggestions() {
  const out = [];
  for (const [path, sug] of Object.entries(currentSuggestions)) {
    if (sug.action !== "quarantine" && sug.action !== "archive") continue;
    if (decisions.has(path)) continue;
    out.push({ path, action: sug.action, target: sug.suggested_path || "" });
  }
  return out;
}

/* 按钮上的条数必须与实际可采纳数一致，否则又是一个「界面说的和实际不一样」。 */
function refreshAdoptHint() {
  const btn = document.getElementById("adoptBtn");
  const hint = document.getElementById("adoptHint");
  if (!btn || !hint) return;

  const items = adoptableSuggestions();
  const quarantine = items.filter((i) => i.action === "quarantine").length;
  const archive = items.length - quarantine;
  btn.textContent = items.length ? `采纳全部建议（${items.length} 条）` : "采纳全部建议";
  btn.disabled = items.length === 0;

  if (items.length) {
    hint.textContent =
      `将填入：隔离 ${quarantine} 项、归档 ${archive} 项。` +
      "只填你还没表态的行，不覆盖你在「决策」列做过的修改；" +
      "填完最后点「执行候选决策」才会真正搬文件。";
  } else if (Object.keys(currentSuggestions).length) {
    hint.textContent = "本轮建议里没有可采纳的（模型只给了 keep / review，或你都已逐条表态）。";
  } else {
    hint.textContent = "本轮没有模型建议。模型是可选增强，不用它也能靠规则 + 人工完成终审。";
  }
}

/* 「采纳全部建议」：把模型明确建议隔离 / 归档的行按建议填进决策。
   只写 decisions 与界面，**绝不执行**——执行仍要用户点「执行候选决策」。
   这是唯一会把模型建议变成决策的入口；默认决策恒为 keep。 */
function adoptSuggestions() {
  const rows = document.getElementById("rows");
  const items = adoptableSuggestions();
  let quarantine = 0;
  for (const item of items) {
    const cur = decisionFor(item.path);
    cur.action = item.action;
    if (item.action === "archive") cur.target_relpath = item.target || cur.target_relpath || "";
    cur.note = "adopted ai suggestion";
    decisions.set(item.path, cur);

    const tr = [...rows.querySelectorAll("tr")].find((r) => {
      const s = r.querySelector("select.act");
      return s && s.dataset.path === item.path;
    });
    if (tr) {
      tr.querySelector("select.act").value = item.action;
      const inp = tr.querySelector("input.tgt");
      if (inp && item.action === "archive") inp.value = cur.target_relpath;
      syncTargetEnabled(tr);
    }
    if (item.action === "quarantine") quarantine += 1;
  }
  setStatus({ adopted: items.length, quarantine, archived: items.length - quarantine });
  refreshAdoptHint();
  // 采纳只改了内存里的决策：执行页的清单、数字、按钮要立刻跟着变，
  // 否则按钮还停在「没有待执行的决策」。
  refreshDecisionViews(currentRunData);
}

/* --------------------------------- 规则 --------------------------------- */

function rulePackId() {
  return document.getElementById("rulePackSelect").value || "default";
}

function matchSummary(rule) {
  const m = rule.match || {};
  const values =
    m.names || m.globs || m.suffixes || m.patterns || (m.type === "empty_dir" ? ["空目录"] : []);
  return `${m.type || "?"}${values.length ? `: ${values.join(", ")}` : ""}`;
}

async function loadRules() {
  const data = await api(`/api/rules/${encodeURIComponent(rulePackId())}`);
  currentPack = data.pack_id;
  const tbody = document.getElementById("ruleRows");
  clear(tbody);
  const purgeSafe = new Set(data.purge_safe_ids || []);
  const rules = data.rules || [];
  if (!rules.length) {
    emptyRow(tbody, 5, "这个规则包里没有规则。换一个规则包看看。");
  }

  for (const r of rules) {
    const tr = document.createElement("tr");
    // 名称在前、内部 id 退成小字：这一页要能看懂「哪条规则」，而不是背编号。
    const tdName = document.createElement("td");
    tdName.appendChild(el("div", { text: r.label || r.id }));
    if (r.label) tdName.appendChild(el("div", { cls: "tiny", text: r.id }));
    tr.appendChild(tdName);
    tr.appendChild(
      el("td", {
        cls: "nowrap",
        text: ACTION_LABELS[r.action] || r.action || "只进候选",
        attrs: ACTION_HINTS[r.action] ? { title: ACTION_HINTS[r.action] } : {},
      }),
    );
    tr.appendChild(el("td", { text: matchSummary(r) }));
    tr.appendChild(el("td", { text: r.reason || "" }));

    const td = document.createElement("td");
    if (r.action === "auto_quarantine") {
      const cb = el("input", { attrs: { type: "checkbox", "data-rule": r.id } });
      cb.checked = rulePurge.has(r.id);
      cb.addEventListener("change", () => {
        if (cb.checked) rulePurge.add(r.id);
        else rulePurge.delete(r.id);
        renderRuleSummary(purgeSafe);
      });
      td.appendChild(cb);
      if (purgeSafe.has(r.id)) td.appendChild(el("span", { cls: "hint purge-badge", text: "建议直删" }));
    } else {
      td.appendChild(el("span", { cls: "hint", text: "不适用" }));
    }
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  const ptbody = document.getElementById("protectRows");
  clear(ptbody);
  const policy = data.protect_policy || [];
  if (!policy.length) {
    emptyRow(ptbody, 5, "没有生效的保护规则——这不应该发生，内置保护清单不该为空。");
  }
  for (const p of policy) {
    const tr = document.createElement("tr");
    tr.appendChild(el("td", { text: p.builtin ? "内置" : "规则包" }));
    tr.appendChild(el("td", { text: p.id }));
    tr.appendChild(el("td", { text: p.match_type }));
    tr.appendChild(el("td", { text: (p.values || []).join(", ") }));
    tr.appendChild(el("td", { text: p.reason }));
    ptbody.appendChild(tr);
  }

  renderRuleSummary(purgeSafe);
}

function renderRuleSummary(purgeSafe) {
  const box = document.getElementById("ruleSummary");
  const list = [...rulePurge].sort();
  const dirty =
    list.length !== savedPurge.size || list.some((x) => !savedPurge.has(x));
  const parts = [
    `规则包 ${currentPack}：共 ${document.querySelectorAll("#ruleRows input[data-rule]").length} 条可直删的自动隔离规则。`,
    list.length
      ? `当前选择 ${list.length} 条直删：${list.join("、")}`
      : "当前没有开启任何直删规则（全部只进隔离区）。",
  ];
  if (purgeSafe && purgeSafe.size) {
    parts.push(`其中标记「建议直删」的有 ${[...purgeSafe].join("、")}。`);
  }
  if (dirty) parts.push("有未保存的改动，点「保存直删配置」生效。");
  box.textContent = parts.join(" ");
}

/* --------------------------------- 设置 --------------------------------- */

function renderModelForm(form) {
  const f = form || {};
  const oc = f.openai_compatible || {};
  const ol = f.ollama || {};
  document.getElementById("provider").value = f.provider || "openai_compatible";
  document.getElementById("baseUrl").value = oc.base_url || "";
  document.getElementById("modelName").value = oc.model || "";
  document.getElementById("apiKeyEnv").value = oc.api_key_env || "";
  document.getElementById("ollamaHost").value = ol.host || "";
  document.getElementById("ollamaModel").value = ol.model || "";
  const s = f.summary || {};
  if (s.max_targets != null) document.getElementById("maxTargets").value = s.max_targets;
  if (s.max_depth != null) document.getElementById("maxDepth").value = s.max_depth;
  document.getElementById("apiKey").value = "";
  toggleProviderFields();
}

function toggleProviderFields() {
  const isOpenAI = document.getElementById("provider").value === "openai_compatible";
  document.getElementById("openaiFields").style.display = isOpenAI ? "block" : "none";
  document.getElementById("ollamaFields").style.display = isOpenAI ? "none" : "block";
}

async function loadSettings() {
  const data = await api("/api/settings");
  const sel = document.getElementById("provider");
  clear(sel);
  for (const p of data.providers || ["openai_compatible", "ollama"]) {
    sel.appendChild(el("option", { text: p, attrs: { value: p } }));
  }
  document.getElementById("useModel").checked = !!data.use_model;
  // 只刷新「已保存」的基准；不动 rulePurge——否则来设置页看一眼就会把
  // 规则页里还没保存的勾选悄悄丢掉。
  savedPurge = new Set(data.auto_purge_rules || []);
  renderModelForm(data.model_form);

  const m = data.model || {};
  const bits = [`配置文件：${m.path || "-"}`];
  bits.push(m.configured ? "已配置" : "尚未配置");
  if (m.provider) bits.push(`provider=${m.provider}`);
  if (m.model) bits.push(`model=${m.model}`);
  if (m.endpoint) bits.push(`endpoint=${m.endpoint}`);
  if (m.provider === "openai_compatible") {
    bits.push(m.api_key_inline ? "已保存密钥（明文落盘）" : "未保存密钥");
    if (m.api_key_env) {
      bits.push(`环境变量 ${m.api_key_env} ${m.api_key_env_set ? "已设置" : "未设置"}`);
    }
  }
  document.getElementById("modelStatus").textContent = bits.join(" · ");
}

/* --------------------------------- 监视 --------------------------------- */

async function refreshWatch() {
  let st;
  try {
    st = await api("/api/watch");
  } catch (e) {
    setStatus(e, { error: true });
    return;
  }
  const cards = document.getElementById("watchCards");
  clear(cards);
  cards.appendChild(card("状态", st.running ? "运行中" : "已停止"));
  cards.appendChild(card("已跑轮次", st.rounds || 0, `间隔 ${st.interval_minutes || 0} 分钟`));
  cards.appendChild(card("当前命中", st.total || 0, st.rules ? `规则包 ${st.rules}` : ""));
  cards.appendChild(card("上次扫描", st.last_at || "-"));

  const box = document.getElementById("watchDetail");
  clear(box);
  if (st.error) box.appendChild(el("div", { cls: "hint", text: `出错：${st.error}` }));
  if ((st.added || []).length) {
    box.appendChild(el("div", { text: `新增 ${st.added.length} 项：` }));
    for (const p of st.added) {
      const line = el("div", { cls: "rowline" });
      line.appendChild(el("code", { text: p }));
      box.appendChild(line);
    }
  }
  if ((st.removed || []).length) {
    box.appendChild(el("div", { text: `消失 ${st.removed.length} 项：` }));
    for (const p of st.removed) {
      const line = el("div", { cls: "rowline" });
      line.appendChild(el("code", { text: p }));
      box.appendChild(line);
    }
  }
  if (!st.added.length && !st.removed.length) {
    box.appendChild(el("div", { text: "监视只扫描不执行；这里会列出每轮的新增与消失。" }));
  }

  if (watchTimer) {
    clearInterval(watchTimer);
    watchTimer = null;
  }
  if (st.running) watchTimer = setInterval(refreshWatch, 2000);
}

/* --------------------------------- 载入 --------------------------------- */

/* run id 是机器时间 + 6 位随机后缀（`2026-10-17T042716+0000-c9874f`）。
   下拉里直接铺这个，用户选不出「哪个是刚才那次」——翻成
   「10-17 12:27 · 命中 4 · 候选 2」才是人看的。 */
function shortTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function runLabel(r) {
  const bits = [];
  const when = shortTime(r.started_at);
  if (when) bits.push(when);
  bits.push(`命中 ${Number(r.auto_hits) || 0}`);
  // 老 run 没有 candidates 字段（后端给 -1）：不显示，而不是显示一个假的 0。
  if (Number(r.candidates) >= 0) bits.push(`候选 ${r.candidates}`);
  if (r.applied) bits.push("已执行");
  return bits.join(" · ");
}

async function loadRuns() {
  const meta = await api("/api/meta");
  ADVICE_KINDS = meta.advice_order;
  const legend = document.getElementById("adviceLegend");
  const adviceSelect = document.getElementById("pendingAdvice");
  clear(legend);
  clear(adviceSelect);
  adviceSelect.appendChild(el("option", { text: "全部", attrs: { value: "" } }));
  for (const kind of ADVICE_KINDS) {
    legend.appendChild(el("span", {
      cls: `advice advice-${kind}`,
      text: meta.advice_labels[kind],
      attrs: { title: meta.advice_hints[kind] },
    }));
    legend.appendChild(document.createTextNode(` ${meta.advice_hints[kind]} `));
    adviceSelect.appendChild(el("option", {
      text: meta.advice_labels[kind],
      attrs: { value: kind, title: meta.advice_hints[kind] },
    }));
  }
  adviceSelect.value = pendingFilter.advice;
  document.getElementById("watchTimeout").value = meta.watch_default_timeout;
  document.getElementById("meta").textContent = `目标根：${meta.root}`;
  document.getElementById("stateDir").textContent = `状态目录：${meta.state_dir}`;
  const { runs } = await api("/api/runs");
  const sel = document.getElementById("runSelect");
  clear(sel);
  for (const r of runs) {
    sel.appendChild(el("option", { text: runLabel(r), attrs: { value: r.id, title: r.id } }));
  }
  const ids = runs.map((r) => r.id);
  if (meta.run_id && ids.includes(meta.run_id)) sel.value = meta.run_id;
  if (sel.value) await loadRun(sel.value);
  else setStatus("还没有任何 run：切到「扫描」页跑一次。");
}

async function loadRun(runId) {
  const switched = currentRun !== runId;
  if (switched) { pendingPage = 1; candPage = 1; }
  currentRun = runId;
  currentPlan = null;
  const data = await api(`/api/runs/${runId}`);
  if (currentRun !== runId) return; // 较慢的旧请求不得盖过新 run。
  currentRunData = data;
  decisions.clear();
  if (switched) {
    keptPaths.clear();
    candPicked.clear();
    quarantined.clear();
  }
  for (const d of data.decisions || []) decisions.set(d.path, d);
  renderPartialWarning(data);
  renderOverview(data);
  renderScanSummary(data);
  renderReview(data);
  renderQuarantine(data);
  // 动作类操作会先 setStatus(结果) 再 loadRun；载入时不再写一次状态，
  // 避免「隔离 3」被「已载入 run」冲掉。但转移摘要需要在新数据到位后
  // 补上「还剩多少」，这里只补、不覆盖。
  finishTransferStatus();
}

async function save() {
  if (!currentRun) throw new Error("还没有选中 run");
  const body = { decisions: [...decisions.values()] };
  const res = await api(`/api/runs/${currentRun}/decisions`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  setStatus(res);
}

/* 候选表批量选择：更新「已选 N 项」与全选框。 */
function refreshCandPicked(shown) {
  const hint = document.getElementById("candPicked");
  if (hint) hint.textContent = `已选 ${candPicked.size} 项`;
  const box = document.getElementById("candAll");
  if (box) {
    const visible = (shown || []).filter((p) => candPicked.has(p)).length;
    box.checked = shown && shown.length > 0 && visible === shown.length;
    box.indeterminate = visible > 0 && visible < (shown || []).length;
  }
}

/* 批量设决策：只写 decisions + 重绘，不调接口、不碰文件。
   真正搬文件只在「执行」页点「执行候选决策」。 */
function applyCandBulk() {
  if (!candPicked.size) {
    setStatus("还没勾选任何候选行：先勾左侧的「选择」。");
    return;
  }
  const action = document.getElementById("candBulkAction").value;
  let n = 0;
  for (const path of candPicked) {
    const cur = decisionFor(path);
    cur.action = action;
    if (action !== "archive") {
      cur.target_relpath = "";
    } else if (!cur.target_relpath) {
      // 批量归档按「archive/原相对路径」落位。只按文件名拼的话，勾中的多个
      // index.js / notes.txt 会互相顶掉，后端只能报「目标已存在」并跳过。
      cur.target_relpath = `archive/${path}`;
    }
    decisions.set(path, cur);
    n += 1;
  }
  const label = DECISION_LABELS[action] || action;
  // 已选可能包含别的页、或已被筛选条件隐藏的行：动作确实作用到它们身上，
  // 报数时必须说明，否则「我只看着 5 行，怎么改了 40 行」。
  const hidden = [...candPicked].filter((p) => !currentCandShown.includes(p)).length;
  setStatus(
    `已把 ${n} 项设为「${label}」${hidden ? `（其中 ${hidden} 项不在当前页）` : ""}（只改决策，未动任何文件）。`,
  );
  renderCandidates(currentRunData);
}

async function previewDecisions() {
  if (!currentRun) return null;
  const runId = currentRun;
  const res = await api(`/api/runs/${runId}/apply-decisions`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dry_run: true, decisions: [...decisions.values()] }),
  });
  if (currentRun !== runId) return null;
  setStatus(res);
  return res;
}

async function apply() {
  if (!currentRun) throw new Error("还没有选中 run");
  // 每行默认「保留不动」，所以「一条都没设就点执行」是很容易发生的事。
  // 直接调接口会静默地什么都不做——用户以为执行了，其实没有。这里必须说出来。
  const moving = [...decisions.values()].filter(
    (d) => d.action === "quarantine" || d.action === "archive",
  );
  if (!moving.length) {
    return setStatus(
      "还没有可执行的决策：每行默认「保留不动」，请先改「决策」列或点「采纳全部建议」。",
    );
  }
  const preview = await previewDecisions();
  if (!preview) return;
  if (!confirm(`后端预览：隔离 ${preview.quarantined} 项、归档 ${preview.archived} 项；保护拦下 ${preview.blocked_by_protect} 项、跳过 ${(preview.skipped || []).length} 项。归档移动到指定位置，不进入隔离区。继续？`)) {
    return undefined;
  }
  await save();
  const res = await api(`/api/runs/${currentRun}/apply-decisions`, { method: "POST" });
  transferStatus(res);
  await loadRun(currentRun);
  return undefined;
}

/* 执行规则自动隔离；勾选「保留」的项随请求发出，后端本轮不动它们并记入 protection.json */
async function applyRules() {
  const runId = currentRun;
  const keep_paths = [...keptPaths];
  const plan = await refreshPlan(); // 确认前重新核对，不沿用过期的数字。
  if (!plan || currentRun !== runId) return;
  if (!plan.would_quarantine) return setStatus(planSummary(plan));
  if (!confirm(`${planSummary(plan)}\n预览之后文件仍可能变化，执行时会再次保护复核。继续？`)) {
    return undefined;
  }
  const res = await api(`/api/runs/${currentRun}/apply-rules`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keep_paths }),
  });
  keptPaths.clear();
  transferStatus(res);
  await loadRun(currentRun);
  return undefined;
}

/* ------------------------------- 事件绑定 ------------------------------- */

document.querySelectorAll(".tabbtn").forEach((b) => {
  b.addEventListener("click", () => showTab(b.dataset.tab));
});

document.getElementById("reloadBtn").onclick = () =>
  loadRun(document.getElementById("runSelect").value);
document.getElementById("runSelect").onchange = (e) => loadRun(e.target.value);
document.getElementById("adoptBtn").onclick = () => adoptSuggestions();
/* 下列按钮都会真动文件或真发请求，一律套忙态：
   点下去立刻有反馈，且期间点不动（重复点「执行自动隔离」是很贵的误操作）。 */
document.getElementById("saveBtn").onclick = () =>
  withBusy(document.getElementById("saveBtn"), "保存中…", () =>
    save().catch((e) => setStatus(e, { error: true })),
  );
document.getElementById("applyBtn").onclick = () =>
  withBusy(document.getElementById("applyBtn"), "执行中…", () =>
    apply().catch((e) => setStatus(e, { error: true })),
  );
document.getElementById("previewRulesBtn").onclick = () =>
  withBusy(document.getElementById("previewRulesBtn"), "核对中…", () =>
    refreshPlan()
      .then((p) => p && setStatus(planSummary(p)))
      .catch((e) => setStatus(e, { error: true })),
  );
document.getElementById("previewDecisionsBtn").onclick = () =>
  withBusy(document.getElementById("previewDecisionsBtn"), "核对中…", () =>
    previewDecisions().catch((e) => setStatus(e, { error: true })),
  );
document.getElementById("applyRulesBtn").onclick = () =>
  withBusy(document.getElementById("applyRulesBtn"), "执行中…", () =>
    applyRules().catch((e) => setStatus(e, { error: true })),
  );

document.getElementById("gotoExecuteBtn").onclick = () => showTab("execute");
document.getElementById("gotoExecuteBtn2").onclick = () => showTab("execute");

document.getElementById("pendingPrev").onclick = () => {
  if (currentRunData && pendingPage > 1) { pendingPage--; renderPending(currentRunData); }
};
document.getElementById("pendingNextPage").onclick = () => {
  if (currentRunData) { pendingPage++; renderPending(currentRunData); }
};
document.getElementById("candPrev").onclick = () => {
  if (currentRunData && candPage > 1) { candPage--; renderCandidates(currentRunData); }
};
document.getElementById("candNextPage").onclick = () => {
  if (currentRunData) { candPage++; renderCandidates(currentRunData); }
};

document.getElementById("pendingAll").onchange = (e) => {
  const all = (currentRunData && currentRunData.pending_auto_hits) || [];
  for (const h of all) {
    if (e.target.checked) keptPaths.add(h.path);
    else keptPaths.delete(h.path);
  }
  renderPending(currentRunData);
};

document.getElementById("candAll").onchange = (e) => {
  for (const p of currentCandShown || []) {
    if (e.target.checked) candPicked.add(p);
    else candPicked.delete(p);
  }
  refreshCandPicked(currentCandShown);
};

document.getElementById("candBulkBtn").onclick = () => applyCandBulk();
document.getElementById("candSelectMatching").onclick = () => {
  for (const path of currentCandMatching) candPicked.add(path);
  if (currentRunData) renderCandidates(currentRunData);
};
document.getElementById("candClearPicked").onclick = () => {
  candPicked.clear();
  if (currentRunData) renderCandidates(currentRunData);
};
document.getElementById("candAdoptSelected").onclick = () => {
  let adopted = 0;
  let skipped = 0;
  for (const path of candPicked) {
    const sug = currentSuggestions[path];
    if (!sug || !["keep", "quarantine", "archive"].includes(sug.action) || decisions.has(path) ||
        (sug.action === "archive" && !sug.suggested_path)) {
      skipped++;
      continue;
    }
    decisions.set(path, { path, action: sug.action, target_relpath: sug.suggested_path || "", note: "adopted ai suggestion" });
    adopted++;
  }
  setStatus(`已按模型建议设置 ${adopted} 项，跳过 ${skipped} 项（无可用建议、已有决策或归档目标缺失）；未执行文件操作。`);
  if (currentRunData) renderCandidates(currentRunData);
};
for (const [id, key] of [["candRule", "rule"], ["candAi", "ai"], ["candDecision", "decision"]]) {
  document.getElementById(id).onchange = (e) => {
    candFilter[key] = e.target.value;
    candPage = 1;
    if (currentRunData) renderCandidates(currentRunData);
  };
}

/* 审阅页筛选：只改显示，不改判定。执行范围永远是全部命中（勾了保留的除外），
   否则「筛了 3 项却搬走 30 项」会像丢了东西。 */
document.getElementById("pendingAdvice").onchange = (e) => {
  pendingFilter.advice = e.target.value;
  pendingPage = 1;
  if (currentRunData) renderPending(currentRunData);
};
document.getElementById("pendingSearch").oninput = (e) => {
  pendingFilter.q = e.target.value.trim().toLowerCase();
  pendingPage = 1;
  if (currentRunData) renderPending(currentRunData);
};
document.getElementById("candSearch").oninput = (e) => {
  candFilter.q = e.target.value.trim().toLowerCase();
  candPage = 1;
  if (currentRunData) renderCandidates(currentRunData);
};
document.getElementById("pendingGroup").onchange = (e) => {
  pendingGrouped = e.target.checked;
  pendingPage = 1;
  if (currentRunData) renderPending(currentRunData);
};

/* 表头排序：同一列再点一次切方向；换列则用该列的默认方向
   （大小「大的在前」、路径「A 在前」、建议「越该清越前」）。 */
for (const th of document.querySelectorAll("#tab-review th.sortable")) {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (pendingSort.key === key) {
      pendingSort.dir *= -1;
    } else {
      pendingSort.key = key;
      pendingSort.dir = SORT_DEFAULT_DIR[key] || 1;
    }
    pendingPage = 1;
    if (currentRunData) renderPending(currentRunData);
  });
}

/* 批量勾选「保留」。三个按钮只改 keptPaths，**不碰任何文件**——
   真正的移动仍然只在「执行自动隔离」里发生。 */
function keepBatch(kind) {
  if (!currentRunData) return;
  const hits = currentRunData.pending_auto_hits || [];
  if (kind === "all") {
    for (const h of hits) keptPaths.add(h.path);
  } else if (kind === "none") {
    keptPaths.clear();
  } else {
    // 「只保留需要判断的」：把拿不准的留下，明确可清的照常执行。
    // 这是最常用的一键——用户不必逐条去认哪些该留。
    keptPaths.clear();
    for (const h of hits) if (adviceKind(h) === "review") keptPaths.add(h.path);
  }
  renderPending(currentRunData);
  setStatus(
    `已勾选保留 ${keptPaths.size} 项（本轮不动）。点「执行自动隔离」才会搬走其余项。`,
  );
}

/* 「复制这段解释」：排查时要把结论发给别人，手选整段很别扭。
   剪贴板 API 只在 https / localhost 下可用（局域网 IP 打开网页就没有），
   所以留一条 `execCommand` 兜底——先选中 <pre> 内容再走老接口。 */
async function copyExplain(btn) {
  // 复制「人话四段 + 技术明细」的合体：发给人看时，人话在前说明结论，
  // 技术明细在后供对方复核——只给技术日志对方也看不懂。
  const body = document.getElementById("explainBody");
  const summary = explainSummaryText();
  const detail = body ? body.textContent || "" : "";
  let text = detail;
  if (summary && detail && detail !== "加载中…") {
    text = `${summary}\n\n—— 技术判定明细 ——\n${detail}`;
  } else if (summary) {
    text = summary;
  }
  if (!text || text === "加载中…") return;

  let ok = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (e) {
    ok = false;
  }
  if (!ok && body) {
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(body);
    sel.removeAllRanges();
    sel.addRange(range);
    try {
      ok = document.execCommand("copy");
    } catch (e) {
      ok = false;
    }
    sel.removeAllRanges();
  }

  const old = btn.textContent;
  btn.textContent = ok ? "已复制" : "复制失败，请手动选择";
  window.setTimeout(() => {
    btn.textContent = old;
  }, 1600);
}

/* 把「这是什么 / 系统会怎么办 / 为什么 / 删了怎么办」拼成纯文本，
   供复制时放在技术明细前面。 */
function explainSummaryText() {
  const box = document.getElementById("explainSummary");
  if (!box) return "";
  const lines = [];
  for (const row of box.querySelectorAll(".explain-row")) {
    const k = row.querySelector(".explain-k");
    const v = row.querySelector(".explain-v");
    if (k && v && v.textContent) lines.push(`${k.textContent}：${v.textContent}`);
  }
  return lines.join("\n");
}

document.getElementById("keepAllBtn").onclick = () => keepBatch("all");
document.getElementById("keepReviewBtn").onclick = () => keepBatch("review");
document.getElementById("keepNoneBtn").onclick = () => keepBatch("none");
document.getElementById("explainClose").onclick = () =>
  document.getElementById("explainDlg").close();
document.getElementById("explainCopy").onclick = (e) => copyExplain(e.currentTarget);
document.getElementById("statusClose").onclick = () => {
  window.clearTimeout(statusTimer);
  document.getElementById("statusbar").hidden = true;
};
document.getElementById("gotoQuarantineBtn").onclick = () => showTab("quarantine");

/* 模型分析（等价于 dirtidy analyze）。
   默认只分析「规则处理完还剩下的候选」：已表态、已隔离/直删的路径不再送出去。
   先取一份范围预览让用户确认，再提交——批量外发不能点了才知道发了什么。 */
document.getElementById("analyzeBtn").onclick = async () => {
  if (!currentRun) return setStatus("还没有选中 run");
  const btn = document.getElementById("analyzeBtn");
  btn.disabled = true;
  try {
    const preview = await api(`/api/runs/${currentRun}/analyze/preview`);
    if (!preview.enabled || !preview.configured) {
      return setStatus("模型分析未启用：先在「设置」页打开开关并保存配置。", { error: true });
    }
    if (!preview.leftover) {
      return setStatus(
        `没有遗留项可分析：本轮候选 ${preview.candidates} 项，已表态 ${preview.decided} 项、` +
          `已隔离或删除 ${preview.removed} 项。规则这一轮已经处理完了。`,
      );
    }
    const sample = (preview.sample || []).slice(0, 5).map((p) => `  ${p}`).join("\n");
    const ok = confirm(
      `模型分析范围（只看遗留项）\n` +
        `· 本轮候选 ${preview.candidates} 项，其中已表态 ${preview.decided} 项、已搬走 ${preview.removed} 项不再发送\n` +
        `· 本次送模型：${preview.leftover} 项${preview.truncated ? `（超过单次上限 ${preview.max_targets}，只分析前 ${preview.will_analyze} 项）` : ""}\n` +
        (sample ? `· 例如：\n${sample}\n` : "") +
        `\n分析结果只作为参考写回审阅页，不会自动搬动或删除文件。继续？`,
    );
    if (!ok) return setStatus("已取消模型分析。");
    setStatus("模型分析中…");
    const meta = await api(`/api/runs/${currentRun}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope: "leftover" }),
    });
    setStatus(
      `模型分析完成：提交 ${meta.targets ?? "?"} 项` +
        `${meta.truncated ? "（已达单次上限，其余未提交）" : ""}` +
        `${meta.unsent ? `（${meta.unsent} 项不是普通文件或已不存在，未发送）` : ""}，` +
        `得到 ${meta.suggestions ?? 0} 条建议。建议只作参考，仍需人工决定。`,
    );
    await loadRun(currentRun);
  } catch (e) {
    setStatus(e, { error: true });
  } finally {
    btn.disabled = false;
  }
};

/* 扫描 */
/* 扫描进度轮询：大目录扫几分钟，状态条一直写着「扫描中…」和卡死没区别，
   用户会去点第二次（于是产生两个 run、两份隔离清单）。
   这里同时显示「已用 N 秒」与 hash 进度——quick 模式下没有 hash，光有时间也算交代。 */
let scanPollSeq = 0;

function stopScanPolling() {
  // 序号自增：让已经发出去、还没回来的那次轮询作废，
  // 否则迟到的响应会把「扫描完成」的结论覆盖回「扫描中…」。
  scanPollSeq += 1;
  if (scanPollTimer) {
    window.clearInterval(scanPollTimer);
    scanPollTimer = null;
  }
}

function startScanPolling() {
  stopScanPolling();
  const seq = scanPollSeq;
  const startedAt = Date.now();
  const tick = async () => {
    let text = `扫描中… 已用 ${Math.max(0, Math.round((Date.now() - startedAt) / 1000))} 秒`;
    try {
      const p = await api("/api/scan/progress");
      if (p && p.total) text += ` · 内容 hash ${p.done}/${p.total}`;
      else if (p && p.phase) text += ` · ${p.phase}`;
    } catch (_e) {
      /* 轮询失败不影响扫描本身，也不该弹错 */
    }
    if (seq !== scanPollSeq) return;
    setStatus(text);
  };
  scanPollTimer = window.setInterval(tick, 800);
  tick();
}

document.getElementById("scanBtn").onclick = async () => {
  const btn = document.getElementById("scanBtn");
  if (!busyOn(btn, "扫描中…")) return; // 已在扫 → 重复点击直接忽略
  setStatus("扫描中…");
  startScanPolling();
  try {
    const protect = document
      .getElementById("scanProtect")
      .value.split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    const res = await api("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        rules: document.getElementById("scanRules").value,
        quick: document.getElementById("scanQuick").checked,
        workers: Number(document.getElementById("scanWorkers").value) || 4,
        protect,
        apply: document.getElementById("scanApply").checked,
      }),
    });
    setStatus(res);
    await loadRuns();
    document.getElementById("runSelect").value = res.run_id;
    await loadRun(res.run_id);
    // 不再自动跳到审阅页：扫描页自己就有结果摘要（renderScanSummary），
    // 用户先看清「扫出了什么」再决定要不要进去逐条确认。自动跳走会让扫描页
    // 永远只剩一行「已生成 run」——用户回头找结果时一无所获。
  } catch (e) {
    setStatus(e, { error: true });
  } finally {
    stopScanPolling();
    busyOff(btn);
  }
};

/* 隔离区 */
async function quarantineAction(kind, payload) {
  const res = await api(`/api/runs/${currentRun}/${kind}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  quarantined.clear();
  transferStatus(res);
  await loadRun(currentRun);
}

/* 隔离区：四个按钮都会真搬/真删，一律套忙态。
   搬几百项时按钮没反馈，用户会以为没点上——而重复点「清空隔离区」是会删空的。 */
const quarantineBtn = (id, label, fn) => {
  const btn = document.getElementById(id);
  btn.onclick = () => withBusy(btn, label, fn);
};

quarantineBtn("restoreSelBtn", "还原中…", () =>
  quarantineAction("restore", { ids: [...quarantined] }).catch((e) =>
    setStatus(e, { error: true }),
  ),
);
quarantineBtn("purgeSelBtn", "删除中…", () => {
  if (!confirm(`彻底删除选中的 ${quarantined.size} 项？此操作不可恢复。`)) return undefined;
  return quarantineAction("purge", { ids: [...quarantined] }).catch((e) =>
    setStatus(e, { error: true }),
  );
});
quarantineBtn("restoreAllBtn", "还原中…", () =>
  quarantineAction("restore", { all: true }).catch((e) => setStatus(e, { error: true })),
);
quarantineBtn("purgeAllBtn", "清空中…", () => {
  if (!confirm("清空隔离区并彻底删除全部条目？此操作不可恢复。")) return undefined;
  return quarantineAction("purge", { all: true }).catch((e) => setStatus(e, { error: true }));
});

/* 规则 */
document.getElementById("ruleLoadBtn").onclick = () =>
  withBusy(document.getElementById("ruleLoadBtn"), "读取中…", () =>
    loadRules().catch((e) => setStatus(e, { error: true })),
  );
document.getElementById("ruleSaveBtn").onclick = () =>
  withBusy(document.getElementById("ruleSaveBtn"), "保存中…", async () => {
    try {
      const res = await api("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auto_purge_rules: [...rulePurge] }),
      });
      savedPurge = new Set(res.auto_purge_rules || []);
      rulePurge = new Set(savedPurge);
      setStatus(res);
      await loadRules();
      if (currentRun) await loadRun(currentRun);
    } catch (e) {
      setStatus(e, { error: true });
    }
  });

/* 设置 */
document.getElementById("provider").onchange = toggleProviderFields;
document.getElementById("saveUseModelBtn").onclick = () =>
  withBusy(document.getElementById("saveUseModelBtn"), "保存中…", async () => {
    try {
      const res = await api("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ use_model: document.getElementById("useModel").checked }),
      });
      setStatus(res);
      await loadSettings();
    } catch (e) {
      setStatus(e, { error: true });
    }
  });
document.getElementById("saveModelBtn").onclick = () =>
  withBusy(document.getElementById("saveModelBtn"), "保存中…", async () => {
    try {
      const res = await api("/api/model", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: document.getElementById("provider").value,
          openai_compatible: {
            base_url: document.getElementById("baseUrl").value,
            model: document.getElementById("modelName").value,
            api_key_env: document.getElementById("apiKeyEnv").value,
            api_key: document.getElementById("apiKey").value,
          },
          ollama: {
            host: document.getElementById("ollamaHost").value,
            model: document.getElementById("ollamaModel").value,
          },
          summary: {
            max_targets: Number(document.getElementById("maxTargets").value) || 500,
            max_depth: Number(document.getElementById("maxDepth").value) || 3,
          },
        }),
      });
      setStatus(res);
      await loadSettings();
    } catch (e) {
      setStatus(e, { error: true });
    }
  });

/* 监视 */
document.getElementById("watchStartBtn").onclick = () =>
  withBusy(document.getElementById("watchStartBtn"), "启动中…", async () => {
    try {
      const protect = document
        .getElementById("watchProtect")
        .value.split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
      const res = await api("/api/watch/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          interval_minutes: Number(document.getElementById("watchInterval").value) || 30,
          timeout: Number(document.getElementById("watchTimeout").value),
          rules: document.getElementById("watchRules").value,
          quick: document.getElementById("watchQuick").checked,
          protect,
          notify: document.getElementById("watchNotify").checked,
        }),
      });
      setStatus(res);
      await refreshWatch();
    } catch (e) {
      setStatus(e, { error: true });
    }
  });
document.getElementById("watchStopBtn").onclick = () =>
  withBusy(document.getElementById("watchStopBtn"), "停止中…", async () => {
    try {
      setStatus(await api("/api/watch/stop", { method: "POST" }));
      await refreshWatch();
    } catch (e) {
      setStatus(e, { error: true });
    }
  });

/* --------------------------------- 启动 --------------------------------- */

async function boot() {
  // 规则包下拉：扫描页与监视页共用同一份清单
  const packs = await api("/api/rules");
  for (const id of ["scanRules", "watchRules", "rulePackSelect"]) {
    const sel = document.getElementById(id);
    clear(sel);
    for (const p of packs.packs || []) {
      sel.appendChild(el("option", { text: p, attrs: { value: p } }));
    }
  }
  // 先把「已保存的直删规则」读进来：规则页的勾选框必须反映后端真实状态，
  // 否则会显示成未开启，一点保存就把已有配置抹掉。
  const settings = await api("/api/settings");
  savedPurge = new Set(settings.auto_purge_rules || []);
  rulePurge = new Set(savedPurge);
  await loadRuns();
  const hash = location.hash.replace("#", "");
  showTab(["overview", "scan", "review", "execute", "quarantine", "rules", "settings", "watch"].includes(hash) ? hash : "overview");
  // 深链：?explain=<相对路径> 直接打开该路径的解释弹窗，方便把「为什么判它」发给别人
  const target = new URLSearchParams(location.search).get("explain");
  if (target) explain(target);
}

boot().catch((e) => setStatus(e, { error: true }));
