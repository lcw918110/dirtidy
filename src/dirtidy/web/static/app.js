/* dirtidy 控制台：扫描 / 审阅 / 执行 / 隔离区 / 规则 / 设置 / 监视
 *
 * 所有动作都打到后端同一份实现（pipeline / quarantine / ai / watch），
 * 网页不自己算规则、不自己判断保护——否则「网页说的和命令行做的不一样」。
 */

let currentRun = null;
const decisions = new Map();
/* 审阅时勾选「保留」的自动隔离项：执行自动隔离时随请求发出，本轮不动它们 */
const keptPaths = new Set();
/* 隔离区里勾选要还原/彻底删除的条目 id */
const quarantined = new Set();
/* 规则页：待保存的直删规则 id 与已保存的，用于标出改动 */
let rulePurge = new Set();
let savedPurge = new Set();
let currentPack = "";
let watchTimer = null;

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
  text.textContent = isError ? String((obj && obj.message) || obj) : humanStatus(obj);
  bar.classList.toggle("error", isError);
  bar.hidden = false;
  window.clearTimeout(statusTimer);
  if (!isError) {
    statusTimer = window.setTimeout(() => {
      bar.hidden = true;
    }, 6000);
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

function card(k, v, s) {
  const box = el("div", { cls: "card" });
  box.appendChild(el("div", { cls: "k", text: k }));
  box.appendChild(el("div", { cls: "v", text: v }));
  if (s) box.appendChild(el("div", { cls: "s", text: s }));
  return box;
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
  document.getElementById("explainBody").textContent = "加载中…";
  dlg.showModal();
  try {
    const data = await api(
      `/api/runs/${currentRun}/explain?path=${encodeURIComponent(path)}`,
    );
    document.getElementById("explainBody").textContent = data.text;
  } catch (e) {
    document.getElementById("explainBody").textContent = String(e);
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
  if (name === "rules") loadRules();
  if (name === "settings") loadSettings();
  if (name === "watch") refreshWatch();
  location.hash = name;
}

/* --------------------------------- 概览 --------------------------------- */

function renderOverview(data) {
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
  document.getElementById("runHint").textContent = meta.applied
    ? "本轮已执行过自动隔离。"
    : "本轮尚未执行自动隔离。";
}

/* ------------------------------- 待自动隔离 ------------------------------- */

/* 待自动隔离：规则已经判定要搬走、但还没执行的项。
   这是「不误删」的终审入口——没有它，用户在网页上根本看不到规则打算动什么。 */
function renderPending(data) {
  const tbody = document.getElementById("pendingRows");
  clear(tbody);
  const hits = data.pending_auto_hits || [];
  const purgeRules = new Set(data.auto_purge_rules || []);
  if (!hits.length) {
    emptyRow(tbody, 6, "本轮没有规则命中项。切到「扫描」页跑一次，或换一个 run。");
  }
  // 命中目录会覆盖其内的文件命中（执行时只搬父目录）。这里把它标出来——
  // 否则页面写着「N 项」而实际只搬走更少，用户按数字核对会以为漏了。
  const dirPrefixes = hits.filter((h) => h.is_dir).map((h) => `${h.path}/`);
  const coveredByParent = (p) => dirPrefixes.some((d) => p.startsWith(d));

  for (const h of hits) {
    const tr = document.createElement("tr");

    const tdKeep = document.createElement("td");
    const cb = el("input", { attrs: { type: "checkbox", "data-path": h.path } });
    cb.checked = keptPaths.has(h.path);
    cb.addEventListener("change", () => {
      if (cb.checked) keptPaths.add(h.path);
      else keptPaths.delete(h.path);
    });
    tdKeep.appendChild(cb);
    tr.appendChild(tdKeep);

    const tdPath = document.createElement("td");
    tdPath.appendChild(el("code", { text: h.path }));
    tdPath.appendChild(whyButton(h.path));
    tr.appendChild(tdPath);

    tr.appendChild(el("td", { text: h.rule_id }));
    tr.appendChild(
      el("td", {
        text: `${h.is_dir ? "目录" : "文件"}${coveredByParent(h.path) ? "（已被上层目录覆盖）" : ""}`,
      }),
    );
    tr.appendChild(el("td", { text: h.reason || "" }));
    tr.appendChild(
      el("td", {
        text: purgeRules.has(h.rule_id) ? "是（搬入后即物理删除）" : "否（只进隔离区）",
      }),
    );
    tbody.appendChild(tr);
  }

  const btn = document.getElementById("applyRulesBtn");
  btn.disabled = hits.length === 0;
  btn.textContent = hits.length ? `执行自动隔离（${hits.length} 项命中）` : "无待隔离项";
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
      document.createTextNode(` ← ${r.protect_rule_id}（${r.protect_reason || ""}）`),
    );
    if (r.intended_rule_id) {
      line.appendChild(document.createTextNode(`；原本想动它的是 ${r.intended_rule_id}`));
    }
    line.appendChild(whyButton(r.path));
    box.appendChild(line);
  }
  if (policy.length) {
    box.appendChild(
      el("div", {
        cls: "hint",
        text: `生效保护策略 ${policy.length} 条：${policy.map((p) => p.id).join("、")}`,
      }),
    );
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

    tr.appendChild(el("td", { text: e.rule_id || "-" }));
    tr.appendChild(el("td", { text: e.is_dir ? "目录" : "文件" }));
    // 用后端实算的 size_actual：清单里的 size 对目录恒为 0，
    // 逐行显示 0 B 而合计显示 293 KB 会自相矛盾。
    tr.appendChild(
      el("td", { text: e.exists === false ? "已不在磁盘" : humanSize(e.size_actual) }),
    );
    tr.appendChild(el("td", { text: e.quarantined_at || "" }));
    tr.appendChild(el("td", { text: e.reason || "" }));
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
      document.createTextNode(` ← 规则 ${p.rule_id || "-"}，于 ${p.quarantined_at || ""} 删除`),
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
    hint.textContent = "未运行模型分析。模型是可选增强：不用它，规则引擎与人工终审一样完整。";
    return;
  }
  hint.textContent = `本次共 ${suggestions.length} 条建议。模型输出只是参考，最终仍以你的决策为准。`;
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

  const rows = document.getElementById("rows");
  clear(rows);
  const suggestions = {};
  for (const s of data.ai_suggestions || []) suggestions[s.path] = s;

  // 非重复组的候选 + AI 建议路径并集
  const paths = new Set();
  for (const c of data.candidates || []) {
    if (c.kind === "duplicate_group") continue;
    for (const p of c.paths || []) paths.add(p);
  }
  for (const p of Object.keys(suggestions)) paths.add(p);

  if (!paths.size) {
    emptyRow(rows, 5, "本轮没有候选文件。用完整扫描（不勾「跳过内容 hash」）才会产出候选与重复组。");
  }
  for (const path of [...paths].sort()) {
    const sug = suggestions[path];
    const d = decisionFor(path);
    const tr = document.createElement("tr");

    const tdPath = document.createElement("td");
    tdPath.appendChild(el("code", { text: path }));
    tdPath.appendChild(whyButton(path));
    tr.appendChild(tdPath);

    tr.appendChild(el("td", { text: sug ? "ai" : "candidate" }));
    tr.appendChild(
      el("td", {
        text: sug ? `${sug.action} / ${sug.asset_type || ""} (${sug.confidence ?? ""})` : "-",
      }),
    );

    const tdAct = document.createElement("td");
    const sel = el("select", { cls: "act", attrs: { "data-path": path } });
    for (const v of ["keep", "quarantine", "archive"]) {
      sel.appendChild(el("option", { text: v, attrs: { value: v } }));
    }
    sel.value = d.action || (sug && sug.action === "quarantine" ? "quarantine" : "keep");
    tdAct.appendChild(sel);
    tr.appendChild(tdAct);

    const tdTgt = document.createElement("td");
    const inp = el("input", {
      cls: "tgt",
      attrs: { "data-path": path, placeholder: "archive/..." },
    });
    inp.value = d.target_relpath || (sug && sug.suggested_path) || "";
    tdTgt.appendChild(inp);
    tr.appendChild(tdTgt);

    rows.appendChild(tr);
  }

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
  for (const c of dupGroups) {
    const box = el("div", { cls: "dup-group" });
    box.appendChild(el("div", { text: c.reason }));
    const groupName = `dup-${(c.meta && c.meta.sha256) || ""}`;
    for (const p of c.paths) {
      const d = decisionFor(p);
      const label = document.createElement("label");
      const radio = el("input", { attrs: { type: "radio", name: groupName, value: p } });
      radio.checked = d.action === "keep";
      label.appendChild(radio);
      label.appendChild(document.createTextNode(" 保留 "));
      label.appendChild(el("code", { text: p }));
      const line = document.createElement("div");
      line.appendChild(label);
      box.appendChild(line);
    }
    const btn = el("button", { text: "其余标记隔离", attrs: { type: "button" } });
    btn.onclick = () => {
      const kept = box.querySelector(`input[name="${CSS.escape(groupName)}"]:checked`);
      if (!kept) return alert("请先选择保留项");
      for (const p of c.paths) {
        decisions.set(p, {
          path: p,
          action: p === kept.value ? "keep" : "quarantine",
          target_relpath: "",
          note: "duplicate pick",
        });
      }
      setStatus({ updated_duplicates: c.paths });
      loadRun(currentRun);
    };
    box.appendChild(btn);
    dups.appendChild(box);
  }

  rows.querySelectorAll(".act").forEach((node) => {
    node.addEventListener("change", () => {
      const path = node.dataset.path;
      const cur = decisionFor(path);
      cur.action = node.value;
      decisions.set(path, cur);
    });
  });
  rows.querySelectorAll(".tgt").forEach((node) => {
    node.addEventListener("change", () => {
      const path = node.dataset.path;
      const cur = decisionFor(path);
      cur.target_relpath = node.value;
      decisions.set(path, cur);
    });
  });
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
    tr.appendChild(el("td", { text: r.id }));
    tr.appendChild(el("td", { text: r.action || "candidate_only" }));
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

async function loadRuns() {
  const meta = await api("/api/meta");
  document.getElementById("meta").textContent = `目标根：${meta.root}`;
  document.getElementById("stateDir").textContent = `状态目录：${meta.state_dir}`;
  const { runs } = await api("/api/runs");
  const sel = document.getElementById("runSelect");
  clear(sel);
  for (const r of runs) sel.appendChild(el("option", { text: r, attrs: { value: r } }));
  if (meta.run_id && runs.includes(meta.run_id)) sel.value = meta.run_id;
  if (sel.value) await loadRun(sel.value);
  else setStatus("还没有任何 run：切到「扫描」页跑一次。");
}

async function loadRun(runId) {
  currentRun = runId;
  const data = await api(`/api/runs/${runId}`);
  for (const d of data.decisions || []) decisions.set(d.path, d);
  renderOverview(data);
  renderReview(data);
  renderQuarantine(data);
  // 这里刻意不写状态条：动作类操作会先 setStatus(结果) 再 loadRun，
  // 若载入时也写一次，用户刚看到的「隔离 3」会立刻被「已载入 run」冲掉。
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

async function apply() {
  await save();
  const res = await api(`/api/runs/${currentRun}/apply-decisions`, { method: "POST" });
  setStatus(res);
  await loadRun(currentRun);
}

/* 执行规则自动隔离；勾选「保留」的项随请求发出，后端本轮不动它们并记入 protection.json */
async function applyRules() {
  const keep_paths = [...keptPaths];
  const res = await api(`/api/runs/${currentRun}/apply-rules`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keep_paths }),
  });
  keptPaths.clear();
  setStatus(res);
  await loadRun(currentRun);
}

/* ------------------------------- 事件绑定 ------------------------------- */

document.querySelectorAll(".tabbtn").forEach((b) => {
  b.addEventListener("click", () => showTab(b.dataset.tab));
});

document.getElementById("reloadBtn").onclick = () =>
  loadRun(document.getElementById("runSelect").value);
document.getElementById("runSelect").onchange = (e) => loadRun(e.target.value);
document.getElementById("saveBtn").onclick = () =>
  save().catch((e) => setStatus(e, { error: true }));
document.getElementById("applyBtn").onclick = () =>
  apply().catch((e) => setStatus(e, { error: true }));
document.getElementById("applyRulesBtn").onclick = () =>
  applyRules().catch((e) => setStatus(e, { error: true }));
document.getElementById("explainClose").onclick = () =>
  document.getElementById("explainDlg").close();
document.getElementById("statusClose").onclick = () => {
  window.clearTimeout(statusTimer);
  document.getElementById("statusbar").hidden = true;
};
document.getElementById("gotoQuarantineBtn").onclick = () => showTab("quarantine");

/* 模型分析（等价于 dirtidy analyze）。未在设置页启用时后端会返回 400，原样展示。 */
document.getElementById("analyzeBtn").onclick = async () => {
  if (!currentRun) return setStatus("还没有选中 run");
  const btn = document.getElementById("analyzeBtn");
  btn.disabled = true;
  setStatus("模型分析中…");
  try {
    setStatus(await api(`/api/runs/${currentRun}/analyze`, { method: "POST" }));
    await loadRun(currentRun);
  } catch (e) {
    setStatus(e, { error: true });
  } finally {
    btn.disabled = false;
  }
};

/* 扫描 */
document.getElementById("scanBtn").onclick = async () => {
  const btn = document.getElementById("scanBtn");
  btn.disabled = true;
  setStatus("扫描中…");
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
    const box = document.getElementById("scanResult");
    clear(box);
    box.appendChild(el("div", { text: `已生成 run：${res.run_id}` }));
    if (res.applied) box.appendChild(el("div", { text: "已按设置执行自动隔离。" }));
    await loadRuns();
    document.getElementById("runSelect").value = res.run_id;
    await loadRun(res.run_id);
    showTab("review");
  } catch (e) {
    setStatus(e, { error: true });
  } finally {
    btn.disabled = false;
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
  setStatus(res);
  await loadRun(currentRun);
}

document.getElementById("restoreSelBtn").onclick = () =>
  quarantineAction("restore", { ids: [...quarantined] }).catch((e) => setStatus(e, { error: true }));
document.getElementById("purgeSelBtn").onclick = () => {
  if (!confirm(`彻底删除选中的 ${quarantined.size} 项？此操作不可恢复。`)) return;
  quarantineAction("purge", { ids: [...quarantined] }).catch((e) => setStatus(e, { error: true }));
};
document.getElementById("restoreAllBtn").onclick = () =>
  quarantineAction("restore", { all: true }).catch((e) => setStatus(e, { error: true }));
document.getElementById("purgeAllBtn").onclick = () => {
  if (!confirm("清空隔离区并彻底删除全部条目？此操作不可恢复。")) return;
  quarantineAction("purge", { all: true }).catch((e) => setStatus(e, { error: true }));
};

/* 规则 */
document.getElementById("ruleLoadBtn").onclick = () =>
  loadRules().catch((e) => setStatus(e, { error: true }));
document.getElementById("ruleSaveBtn").onclick = async () => {
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
};

/* 设置 */
document.getElementById("provider").onchange = toggleProviderFields;
document.getElementById("saveUseModelBtn").onclick = async () => {
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
};
document.getElementById("saveModelBtn").onclick = async () => {
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
};

/* 监视 */
document.getElementById("watchStartBtn").onclick = async () => {
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
};
document.getElementById("watchStopBtn").onclick = async () => {
  try {
    setStatus(await api("/api/watch/stop", { method: "POST" }));
    await refreshWatch();
  } catch (e) {
    setStatus(e, { error: true });
  }
};

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
  showTab(["overview", "scan", "review", "quarantine", "rules", "settings", "watch"].includes(hash) ? hash : "overview");
  // 深链：?explain=<相对路径> 直接打开该路径的解释弹窗，方便把「为什么判它」发给别人
  const target = new URLSearchParams(location.search).get("explain");
  if (target) explain(target);
}

boot().catch((e) => setStatus(e, { error: true }));
