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

function render(data) {
  const rows = document.getElementById("rows");
  rows.innerHTML = "";
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
    tr.innerHTML = `
      <td><code>${path}</code></td>
      <td>${sug ? "ai" : "candidate"}</td>
      <td>${sug ? `${sug.action} / ${sug.asset_type || ""} (${sug.confidence ?? ""})` : "-"}</td>
      <td>
        <select data-path="${path}" class="act">
          <option value="keep">keep</option>
          <option value="quarantine">quarantine</option>
          <option value="archive">archive</option>
        </select>
      </td>
      <td><input data-path="${path}" class="tgt" placeholder="archive/..." value="${d.target_relpath || (sug && sug.suggested_path) || ""}" /></td>
    `;
    rows.appendChild(tr);
    tr.querySelector(".act").value =
      d.action || (sug && sug.action === "quarantine" ? "quarantine" : "keep");
  }

  // 重复组：单选保留项，其余可一键标 quarantine
  const dups = document.getElementById("dups");
  dups.innerHTML = "";
  for (const c of data.candidates || []) {
    if (c.kind !== "duplicate_group") continue;
    const box = document.createElement("div");
    box.className = "dup-group";
    box.innerHTML = `<div>${c.reason}</div>`;
    for (const p of c.paths) {
      const d = decisionFor(p);
      const line = document.createElement("div");
      line.innerHTML = `<label><input type="radio" name="dup-${c.meta && c.meta.sha256}" value="${p}" ${d.action === "keep" ? "checked" : ""}/> 保留 <code>${p}</code></label>`;
      box.appendChild(line);
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "其余标记隔离";
    btn.onclick = () => {
      const name = `dup-${c.meta && c.meta.sha256}`;
      const kept = box.querySelector(`input[name="${name}"]:checked`);
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

  rows.querySelectorAll(".act").forEach((el) => {
    el.addEventListener("change", () => {
      const path = el.dataset.path;
      const cur = decisionFor(path);
      cur.action = el.value;
      decisions.set(path, cur);
    });
  });
  rows.querySelectorAll(".tgt").forEach((el) => {
    el.addEventListener("change", () => {
      const path = el.dataset.path;
      const cur = decisionFor(path);
      cur.target_relpath = el.value;
      decisions.set(path, cur);
    });
  });
}

async function loadRuns() {
  const meta = await api("/api/meta");
  document.getElementById("meta").textContent = `root: ${meta.root}`;
  const { runs } = await api("/api/runs");
  const sel = document.getElementById("runSelect");
  sel.innerHTML = runs.map((r) => `<option value="${r}">${r}</option>`).join("");
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
