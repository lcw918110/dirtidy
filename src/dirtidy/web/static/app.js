/* dirtidy 审阅页：加载 run → 编辑决策 → 保存 / 执行 */

let currentRun = null;
const decisions = new Map();
/* 审阅时勾选「保留」的自动隔离项：执行自动隔离时随请求发出，本轮不动它们 */
const keptPaths = new Set();

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function setStatus(obj) {
  document.getElementById("status").textContent =
    typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
}

function decisionFor(path) {
  return decisions.get(path) || { path, action: "keep", target_relpath: "", note: "" };
}

/* 统一用 DOM API 建节点：文件名/路径一律走 textContent 或 setAttribute，
   绝不拼 HTML 字符串，避免恶意文件名（如从压缩包解出的文件）注入脚本。 */
function el(tag, { text, cls, attrs } = {}) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = String(text);
  if (cls) node.className = cls;
  if (attrs) for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

/* 待自动隔离：规则已经判定要搬走、但还没执行的项。
   这是「不误删」的终审入口——没有它，用户在网页上根本看不到规则打算动什么。 */
function renderPending(data) {
  const tbody = document.getElementById("pendingRows");
  tbody.replaceChildren();
  const hits = data.pending_auto_hits || [];

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
    tr.appendChild(tdPath);

    tr.appendChild(el("td", { text: h.rule_id }));
    tr.appendChild(el("td", { text: h.is_dir ? "目录" : "文件" }));
    tr.appendChild(el("td", { text: h.reason || "" }));
    tbody.appendChild(tr);
  }

  const btn = document.getElementById("applyRulesBtn");
  btn.disabled = hits.length === 0;
  btn.textContent = hits.length ? `执行自动隔离（${hits.length} 项）` : "无待隔离项";
}

function renderProtection(data) {
  const box = document.getElementById("protection");
  box.replaceChildren();
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

function renderQuarantined(data) {
  const box = document.getElementById("quarantined");
  box.replaceChildren();
  const entries = data.quarantine_manifest || [];
  if (!entries.length) {
    box.appendChild(el("div", { text: "尚无隔离项。" }));
    return;
  }
  for (const e of entries) {
    const line = el("div", { cls: "rowline" });
    line.appendChild(el("code", { text: e.original_path }));
    line.appendChild(document.createTextNode(` → ${e.quarantine_path}`));
    if (e.rule_id) line.appendChild(document.createTextNode(`（${e.rule_id}）`));
    box.appendChild(line);
  }
}

function render(data) {
  renderPending(data);
  renderProtection(data);
  renderQuarantined(data);

  const rows = document.getElementById("rows");
  rows.replaceChildren();
  const suggestions = {};
  for (const s of data.ai_suggestions || []) suggestions[s.path] = s;

  // 非重复组的候选 + AI 建议路径并集
  const paths = new Set();
  for (const c of data.candidates || []) {
    if (c.kind === "duplicate_group") continue;
    for (const p of c.paths || []) paths.add(p);
  }
  for (const p of Object.keys(suggestions)) paths.add(p);

  for (const path of [...paths].sort()) {
    const sug = suggestions[path];
    const d = decisionFor(path);
    const tr = document.createElement("tr");

    const tdPath = document.createElement("td");
    tdPath.appendChild(el("code", { text: path }));
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
  dups.replaceChildren();
  for (const c of data.candidates || []) {
    if (c.kind !== "duplicate_group") continue;
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

async function loadRuns() {
  const meta = await api("/api/meta");
  document.getElementById("meta").textContent = `root: ${meta.root}`;
  const { runs } = await api("/api/runs");
  const sel = document.getElementById("runSelect");
  sel.replaceChildren();
  for (const r of runs) sel.appendChild(el("option", { text: r, attrs: { value: r } }));
  if (meta.run_id && runs.includes(meta.run_id)) sel.value = meta.run_id;
  if (sel.value) await loadRun(sel.value);
}

async function loadRun(runId) {
  currentRun = runId;
  const data = await api(`/api/runs/${runId}`);
  for (const d of data.decisions || []) decisions.set(d.path, d);
  render(data);
  setStatus({ loaded: runId, candidates: (data.candidates || []).length });
}

async function save() {
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

document.getElementById("reloadBtn").onclick = () =>
  loadRun(document.getElementById("runSelect").value);
document.getElementById("saveBtn").onclick = save;
document.getElementById("applyBtn").onclick = apply;
document.getElementById("applyRulesBtn").onclick = applyRules;
document.getElementById("runSelect").onchange = (e) => loadRun(e.target.value);
loadRuns().catch((e) => setStatus(String(e)));
