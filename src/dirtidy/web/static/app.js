/* dirtidy 审阅页：加载 run → 编辑决策 → 保存 / 执行 */

let currentRun = null;
const decisions = new Map();

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

function render(data) {
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

document.getElementById("reloadBtn").onclick = () =>
  loadRun(document.getElementById("runSelect").value);
document.getElementById("saveBtn").onclick = save;
document.getElementById("applyBtn").onclick = apply;
document.getElementById("runSelect").onchange = (e) => loadRun(e.target.value);
loadRuns().catch((e) => setStatus(String(e)));
