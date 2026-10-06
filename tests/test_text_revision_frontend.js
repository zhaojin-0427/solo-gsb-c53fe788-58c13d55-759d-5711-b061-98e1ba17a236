// 逐条文案修订的前端回归（node 直接运行，无框架）：
// 用最小 DOM/fetch 桩在 vm 中加载 app/static/app.js，验证——
// - 行内编辑：进入编辑记录 baseText（进入时看到的生效文案），输入实时同步
//   草稿，重绘不丢草稿；取消退出编辑；
// - 保存：PUT 携带 {text: 草稿, base_text: baseText}；成功后退出编辑、
//   页面文案更新（CPS/字素列随详情重绘）；
// - 旧页面识别：另一标签页先改过（服务端 409）→ 不覆盖，退出编辑并自动
//   拉取最新详情显示最新文案；400（空白稿等校验失败）保持编辑态可就地修正；
// - 恢复原文：POST reset 携带 base_text；409 同样刷新显示最新文案；
// - 详情在编辑期间被外部刷新（生效文案已变）→ 自动退出编辑，不基于过期
//   文案继续编辑。
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const APP_JS = path.join(__dirname, "..", "app", "static", "app.js");

// ---------- 最小 DOM 桩 ----------
function makeEl(tag = "div") {
  const el = {
    tagName: tag,
    textContent: "",
    value: "",
    href: "",
    files: [],
    children: [],
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    listeners: {},
    addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    reset() { this.files = []; },
    closest() { return null; },
    querySelector() { return makeEl(); },
    querySelectorAll() { return []; },
  };
  // innerHTML 赋值即替换内容：清空已挂载的子节点（render 重绘语义）
  let html = "";
  Object.defineProperty(el, "innerHTML", {
    get() { return html; },
    set(v) { html = String(v); el.children = []; },
  });
  return el;
}

const bySelector = new Map();
const documentStub = {
  querySelector(sel) {
    if (!bySelector.has(sel)) bySelector.set(sel, makeEl());
    return bySelector.get(sel);
  },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl(tag); },
};

// ---------- fetch 桩 ----------
const pending = [];
const fetchLog = [];
function fetchStub(url, opts = {}) {
  fetchLog.push({ url, method: opts.method || "GET", opts });
  return new Promise((resolve) => pending.push({ url, opts, resolve }));
}
function popPending(substr) {
  for (let i = pending.length - 1; i >= 0; i--) {
    if (pending[i].url.includes(substr)) return pending.splice(i, 1)[0];
  }
  throw new Error(`没有在途请求匹配 ${substr}`);
}
function popPendingExact(url) {
  for (let i = pending.length - 1; i >= 0; i--) {
    if (pending[i].url === url) return pending.splice(i, 1)[0];
  }
  throw new Error(`没有在途请求匹配 ${url}`);
}
function hasPendingExact(url) {
  return pending.some((r) => r.url === url);
}
function respondJson(req, body, ok = true, status = 200) {
  req.resolve({ ok, status, json: async () => body });
}
const flush = () => new Promise((r) => setImmediate(r));

class FormDataStub {
  constructor() { this.parts = []; }
  append(k, v) { this.parts.push([k, v]); }
}

// ---------- 加载 app.js ----------
const src = fs.readFileSync(APP_JS, "utf8") +
  "\n;globalThis.__app = { state, render, syncTextEdit };";
const sandbox = {
  document: documentStub,
  fetch: fetchStub,
  FormData: FormDataStub,
  setTimeout: () => 0,
  clearTimeout: () => {},
  console,
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "app.js" });
const app = sandbox.__app;
const { state } = app;

// ---------- 测试工具 ----------
const settingsForm = documentStub.querySelector("#settingsForm");
settingsForm.cps_limit = makeEl("input");
settingsForm.min_gap_ms = makeEl("input");
settingsForm.tolerance_ms = makeEl("input");

function cue(uid, text, opts = {}) {
  return {
    uid, cue_id: String(uid), ord: 1,
    start_ms: 0, end_ms: 1000, duration_ms: 1000,
    text, graphemes: opts.graphemes ?? text.length,
    raw_hash: "abcdef0123456789".repeat(4),
    cps: opts.cps ?? 1.0, cps_ok: true,
    overlap_with: null, cross_shot: false,
    proposal_start_ms: 0, proposal_end_ms: 1000,
    adopted: false, adopted_start_ms: null, adopted_end_ms: null,
    imported_text: opts.imported ?? text,
    imported_graphemes: opts.importedGraphemes ?? 1,
    text_edited: opts.edited ?? false,
  };
}

function proj(id, hash, cues) {
  return {
    id, name: `P${id}`, cps_limit: 21, min_gap_ms: 40, tolerance_ms: 200,
    created_at: "", timeline_hash: hash,
    shots: [], cues: [],
    analysis: {
      cues, chains: [], intrinsic: [],
      stats: { cue_count: cues.length, shot_count: 0, gap_violations: 0,
               cps_violations: 0, cross_shot_count: 0, proposal_count: 1,
               proposal_moved_count: 0, conflict_cue_count: 0,
               total_shift_ms: 0 },
    },
    gap_review: null,
  };
}

const cueTable = documentStub.querySelector("#cueTable");

// 浏览器语义：同一事件分发给该元素上注册的全部监听器
function dispatchClick(target) {
  const ev = { target };
  return Promise.all((cueTable.listeners.click || []).map((fn) => fn(ev)));
}
function dispatchInput(target) {
  const ev = { target };
  (cueTable.listeners.input || []).forEach((fn) => fn(ev));
}
function textBtn(act, uid) {
  const btn = { dataset: { textAct: act, uid: String(uid) } };
  return { closest: (sel) => (sel === "button[data-text-act]" ? btn : null) };
}
function draftTa(uid, value) {
  const ta = { dataset: { textDraft: String(uid) }, value };
  return { closest: (sel) => (sel === "textarea[data-text-draft]" ? ta : null) };
}
function tbodyRows() {
  return documentStub.querySelector("#cueTable tbody").children;
}

async function test_edit_save_success() {
  state.project = proj(1, "h1", [cue(5, "甲")]);
  state.textEdit = null;
  app.render();
  // 进入编辑：记录 baseText 为当前生效文案，草稿初值即生效文案
  await dispatchClick(textBtn("edit", 5));
  assert.ok(state.textEdit, "应进入编辑态");
  assert.strictEqual(state.textEdit.uid, 5);
  assert.strictEqual(state.textEdit.baseText, "甲");
  assert.strictEqual(state.textEdit.draft, "甲");
  // 编辑态渲染出 textarea；输入实时同步草稿（重绘不丢）
  app.render();
  assert.ok(tbodyRows()[0].innerHTML.includes("<textarea"),
            "编辑态应渲染行内 textarea");
  dispatchInput(draftTa(5, "新文案"));
  assert.strictEqual(state.textEdit.draft, "新文案");
  app.render();
  assert.ok(tbodyRows()[0].innerHTML.includes("新文案"),
            "重绘后 textarea 应保留草稿");
  // 保存：PUT 携带草稿与 base_text
  const p = dispatchClick(textBtn("save", 5));
  const req = popPending("/api/projects/1/cues/5/text");
  assert.strictEqual(req.opts.method, "PUT");
  assert.deepStrictEqual(JSON.parse(req.opts.body),
                         { text: "新文案", base_text: "甲" });
  respondJson(req, proj(1, "h2", [cue(5, "新文案",
                                      { edited: true, imported: "甲" })]));
  await p; await flush();
  assert.strictEqual(state.textEdit, null, "保存成功后退出编辑");
  assert.strictEqual(state.project.analysis.cues[0].text, "新文案");
  assert.strictEqual(state.project.timeline_hash, "h2");
  console.log("frontend edit -> save with base_text ok");
}

async function test_save_conflict_409_refreshes_latest() {
  // 另一标签页先把「甲」改成了「别人的修改」；本页仍基于「甲」保存 → 409，
  // 不得覆盖，退出编辑并自动拉取最新详情显示最新文案
  state.project = proj(1, "h2", [cue(5, "甲")]);
  state.textEdit = null;
  app.render();
  await dispatchClick(textBtn("edit", 5));
  dispatchInput(draftTa(5, "我的修改"));
  const p = dispatchClick(textBtn("save", 5));
  respondJson(popPending("/api/projects/1/cues/5/text"),
              { detail: "该字幕文案已在其他页面被修改，请基于最新文案重新编辑" },
              false, 409);
  await p; await flush();
  assert.strictEqual(state.textEdit, null, "409 后退出编辑");
  assert.ok(hasPendingExact("/api/projects/1"),
            "409 后必须自动拉取最新详情");
  respondJson(popPendingExact("/api/projects/1"),
              proj(1, "h3", [cue(5, "别人的修改",
                                 { edited: true, imported: "甲" })]));
  await flush();
  assert.strictEqual(state.project.analysis.cues[0].text, "别人的修改",
                     "页面应显示最新文案而非本地草稿");
  console.log("frontend save 409 -> no overwrite + refresh latest ok");
}

async function test_save_blank_400_keeps_editing() {
  state.project = proj(1, "h3", [cue(5, "甲")]);
  state.textEdit = null;
  app.render();
  await dispatchClick(textBtn("edit", 5));
  dispatchInput(draftTa(5, "   "));
  const before = fetchLog.length;
  const p = dispatchClick(textBtn("save", 5));
  respondJson(popPending("/api/projects/1/cues/5/text"),
              { detail: "文案为空：去标签、折叠空白后必须至少保留一个可见字素" },
              false, 400);
  await p; await flush();
  assert.ok(state.textEdit && state.textEdit.uid === 5,
            "400 校验失败应保持编辑态以便就地修正");
  assert.ok(!hasPendingExact("/api/projects/1"),
            "400 不应触发详情刷新");
  // 取消：退出编辑，不发请求
  const n = fetchLog.length;
  await dispatchClick(textBtn("cancel", 5));
  assert.strictEqual(state.textEdit, null);
  assert.strictEqual(fetchLog.length, n, "取消不应发请求");
  console.log("frontend blank 400 keeps editing; cancel silent ok");
}

async function test_reset_success_and_409() {
  // 已修订的 cue：恢复原文携带当前生效文案作 base_text
  state.project = proj(1, "h4", [cue(5, "修订稿",
                                     { edited: true, imported: "甲" })]);
  state.textEdit = null;
  app.render();
  assert.ok(tbodyRows()[0].innerHTML.includes("已修订"),
            "已修订条目应显示徽标");
  assert.ok(tbodyRows()[0].innerHTML.includes("导入原文：甲"),
            "已修订条目应显示导入原文");
  const p = dispatchClick(textBtn("reset", 5));
  const req = popPending("/api/projects/1/cues/5/text/reset");
  assert.strictEqual(req.opts.method, "POST");
  assert.deepStrictEqual(JSON.parse(req.opts.body), { base_text: "修订稿" });
  respondJson(req, proj(1, "h5", [cue(5, "甲")]));
  await p; await flush();
  assert.strictEqual(state.project.analysis.cues[0].text, "甲");

  // 恢复时另一标签页已先改 → 409：不覆盖，刷新显示最新文案
  state.project = proj(1, "h5", [cue(5, "修订稿V2",
                                     { edited: true, imported: "甲" })]);
  app.render();
  const p2 = dispatchClick(textBtn("reset", 5));
  respondJson(popPending("/api/projects/1/cues/5/text/reset"),
              { detail: "该字幕文案已在其他页面被修改，请基于最新文案重新操作" },
              false, 409);
  await p2; await flush();
  respondJson(popPendingExact("/api/projects/1"),
              proj(1, "h6", [cue(5, "他人新修订",
                                 { edited: true, imported: "甲" })]));
  await flush();
  assert.strictEqual(state.project.analysis.cues[0].text, "他人新修订",
                     "409 后应显示最新文案");
  console.log("frontend reset + reset 409 refresh ok");
}

async function test_external_refresh_exits_editing() {
  // 编辑期间详情被外部刷新（另一标签页改动同步到本页）：生效文案已变 →
  // 自动退出编辑，不基于过期文案继续编辑
  state.project = proj(1, "h6", [cue(5, "甲")]);
  state.textEdit = null;
  app.render();
  await dispatchClick(textBtn("edit", 5));
  assert.ok(state.textEdit);
  state.project = proj(1, "h7", [cue(5, "乙", { edited: true, imported: "甲" })]);
  app.render();
  assert.strictEqual(state.textEdit, null,
                     "生效文案已在别处变化时必须退出编辑");
  // 文案未变的普通重绘（如统计条刷新）保持编辑态
  state.project = proj(1, "h7", [cue(5, "丙")]);
  app.render();
  await dispatchClick(textBtn("edit", 5));
  dispatchInput(draftTa(5, "丙-草稿"));
  state.project = proj(1, "h7", [cue(5, "丙")]);
  app.render();
  assert.ok(state.textEdit && state.textEdit.draft === "丙-草稿",
            "文案未变的重绘应保持编辑态与草稿");
  state.textEdit = null;
  console.log("frontend external refresh exits editing ok");
}

async function main() {
  await test_edit_save_success();
  await test_save_conflict_409_refreshes_latest();
  await test_save_blank_400_keeps_editing();
  await test_reset_success_and_409();
  await test_external_refresh_exits_editing();
  console.log("frontend text revision regression ok");
}

main().catch((e) => { console.error(e); process.exit(1); });
