const PREVIEW_STORAGE_KEY = "documentsPreviewStateV6";

function previewData() {
  return window.DOCUMENTS_PREVIEW_DATA || { clients: {}, products: {}, vendors: [], responseFiles: [], serviceLabels: {}, monthlyPlanSettings: {} };
}

function normalizeState(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { items: {}, assignments: {}, vendorDraft: null, completedDocs: {} };
  }
  if (raw.items || raw.assignments || Object.prototype.hasOwnProperty.call(raw, "vendorDraft") || raw.completedDocs) {
    return {
      items: raw.items || {},
      assignments: raw.assignments || {},
      vendorDraft: raw.vendorDraft || null,
      completedDocs: raw.completedDocs || {},
    };
  }
  return { items: raw, assignments: {}, vendorDraft: null, completedDocs: {} };
}

function loadPreviewState() {
  try {
    return normalizeState(JSON.parse(window.localStorage.getItem(PREVIEW_STORAGE_KEY) || "{}"));
  } catch (error) {
    return { items: {}, assignments: {}, vendorDraft: null, completedDocs: {} };
  }
}

function savePreviewState(state) {
  window.localStorage.setItem(PREVIEW_STORAGE_KEY, JSON.stringify(state));
}

function getItemState(productId, fallbackStatus = "認証待ち") {
  const state = loadPreviewState();
  return state.items[productId] || { status: fallbackStatus, unavailable: false };
}

function setItemState(productId, patch) {
  const state = loadPreviewState();
  const current = state.items[productId] || {};
  state.items[productId] = { ...current, ...patch };
  savePreviewState(state);
  return state.items[productId];
}

function setVendorDraft(vendorDraft) {
  const state = loadPreviewState();
  state.vendorDraft = vendorDraft;
  savePreviewState(state);
}

function getVendorDraft() {
  return loadPreviewState().vendorDraft || null;
}

function setAssignment(productId, assignment) {
  const state = loadPreviewState();
  state.assignments[productId] = assignment;
  savePreviewState(state);
}

function getAssignments() {
  return loadPreviewState().assignments || {};
}

function getCompletedDocs() {
  return loadPreviewState().completedDocs || {};
}

function setCompletedDoc(doc) {
  const state = loadPreviewState();
  state.completedDocs = state.completedDocs || {};
  state.completedDocs[doc.id] = doc;
  savePreviewState(state);
  return doc;
}

function removeCompletedDoc(docId) {
  const state = loadPreviewState();
  const doc = state.completedDocs?.[docId];
  if (!doc) return null;
  delete state.completedDocs[docId];
  savePreviewState(state);
  return doc;
}

function defaultAssignments() {
  const assignments = {};
  (previewData().responseFiles || []).forEach((fileInfo) => {
    (fileInfo.items || []).forEach((item) => {
      const selected = findClientByName(item.assigned_client || "");
      if (!selected) return;
      const [clientId, client] = selected;
      assignments[item.product] = {
        clientId,
        clientName: client.name,
        service: fileInfo.service,
        source: fileInfo.label,
        price: item.price,
      };
    });
  });
  return assignments;
}

function getQueryParam(name) {
  const params = new URLSearchParams(window.location.search);
  return params.get(name) || "";
}

function showInlineNotice(id, message) {
  const target = document.getElementById(id);
  if (!target) return;
  target.textContent = message;
  target.hidden = false;
}

function applySearchFilter(inputId, selector, emptyId) {
  const input = document.getElementById(inputId);
  const query = (input?.value || "").trim().toLowerCase();
  const cards = Array.from(document.querySelectorAll(selector));
  let visibleCount = 0;
  cards.forEach((card) => {
    const baseVisible = (card.dataset.baseVisible || "true") === "true";
    const haystack = (card.dataset.search || card.textContent || "").toLowerCase();
    const matches = !query || haystack.includes(query);
    const visible = baseVisible && matches;
    card.style.display = visible ? "" : "none";
    if (visible) visibleCount += 1;
  });
  const empty = document.getElementById(emptyId);
  if (empty) empty.hidden = visibleCount > 0;
}

function applyHistoryFilters() {
  const query = (document.getElementById("history-title-search")?.value || "").trim().toLowerCase();
  const month = document.getElementById("history-month-filter")?.value || "";
  const from = document.getElementById("history-date-from")?.value || "";
  const to = document.getElementById("history-date-to")?.value || "";
  const cards = Array.from(document.querySelectorAll(".js-history-card"));
  let visibleCount = 0;
  cards.forEach((card) => {
    const haystack = (card.dataset.search || card.textContent || "").toLowerCase();
    const date = card.dataset.date || "";
    const cardMonth = card.dataset.month || "";
    const matchesText = !query || haystack.includes(query);
    const matchesMonth = !month || cardMonth === month;
    const matchesFrom = !from || date >= from;
    const matchesTo = !to || date <= to;
    const visible = matchesText && matchesMonth && matchesFrom && matchesTo;
    card.style.display = visible ? "" : "none";
    if (visible) visibleCount += 1;
  });
  const empty = document.getElementById("history-empty");
  if (empty) empty.hidden = visibleCount > 0;
}

function clearHistoryFilters() {
  ["history-title-search", "history-month-filter", "history-date-from", "history-date-to"].forEach((id) => {
    const input = document.getElementById(id);
    if (input) input.value = "";
  });
  applyHistoryFilters();
}

function statusClass(label) {
  switch (label) {
    case "認証待ち": return "pending";
    case "認証済み": return "approved";
    case "査定中": return "appraising";
    case "出品中": return "listing";
    case "入金待ち": return "payment";
    case "完了": return "completed";
    case "受付不可": return "unavailable";
    default: return "approved";
  }
}

function serviceLabel(service) {
  return previewData().serviceLabels?.[service] || service;
}

function computeStageCounts(stage) {
  const counts = { all: 0, wholesale: 0, auction: 0, simultaneous: 0 };
  const products = Object.entries(previewData().products || {});
  if (stage === "stage1") {
    products.forEach(([productId, product]) => {
      const current = getItemState(productId, product.status || "認証待ち");
      if (current.unavailable) return;
      counts[product.service] += 1;
    });
  } else if (stage === "stage2") {
    products.forEach(([productId, product]) => {
      const current = getItemState(productId, product.status || "認証待ち");
      if (current.unavailable || current.stage2Completed) return;
      const expected = product.service === "simultaneous" ? "出品中" : "査定中";
      if (current.status === expected) {
        counts[product.service] += 1;
      }
    });
  } else if (stage === "stage3") {
    (previewData().responseFiles || []).forEach((fileInfo) => {
      counts[fileInfo.service] += 1;
    });
  } else if (stage === "stage4") {
    buildReturnGroups().forEach((group) => {
      counts[group.service] += 1;
    });
  }
  counts.all = counts.wholesale + counts.auction + counts.simultaneous;
  return counts;
}

function updateStageTabCounts() {
  const tabs = Array.from(document.querySelectorAll("[data-stage-tab][data-service-key]"));
  if (!tabs.length) return;
  const cache = {};
  tabs.forEach((tab) => {
    const stage = tab.dataset.stageTab;
    const key = tab.dataset.serviceKey;
    if (!cache[stage]) cache[stage] = computeStageCounts(stage);
    const countNode = tab.querySelector(".service-tab-count");
    if (countNode) countNode.textContent = cache[stage][key] ?? 0;
  });
}

function formatYen(value) {
  const amount = Number(value) || 0;
  return `¥${amount.toLocaleString("ja-JP")}`;
}

function monthlyPlanFromItemCount(itemCount) {
  const settings = previewData().monthlyPlanSettings || {};
  const count = Number(itemCount) || 0;
  const planFor = (suffix, fallbackPlan, fallbackFee, rangeLabel) => {
    const feeValue = settings[`monthly_fee_${suffix}`];
    const fee = Number(feeValue ?? fallbackFee) || 0;
    return {
      plan: settings[`monthly_plan_${suffix}`] || fallbackPlan,
      fee,
      range: rangeLabel,
      itemCount: count,
      isCustom: suffix === "over" && fee <= 0,
    };
  };
  if (count <= 20) return planFor("20", "ライト", 2980, "0〜20商品");
  if (count <= 50) return planFor("50", "スタンダード", 5980, "21〜50商品");
  if (count <= 100) return planFor("100", "プロ", 9800, "51〜100商品");
  if (count <= 300) return planFor("300", "ビジネス", 19800, "101〜300商品");
  return planFor("over", "エンタープライズ", 0, "301商品以上");
}

function formatMonthlyPlanFee(data) {
  return data?.monthlyPlanCustom ? "個別相談" : formatYen(data?.monthlyFee || data?.fee || 0);
}

function shortProductName(value, limit = 18) {
  const clean = String(value || "").trim();
  if (!clean) return "商品名未設定";
  return clean.length > limit ? `${clean.slice(0, limit)}…` : clean;
}

function settlementSupportFeeFromSale(saleAmount) {
  const amount = Number(saleAmount) || 0;
  if (amount <= 0) return 0;
  if (amount <= 19999) return 500;
  if (amount <= 49999) return 650;
  if (amount <= 99999) return 800;
  return 1000;
}

function settlementSupportTierLabel(saleAmount) {
  const amount = Number(saleAmount) || 0;
  if (amount <= 0) return "売上未入力";
  if (amount <= 19999) return "2万円以下";
  if (amount <= 49999) return "5万円以下";
  if (amount <= 99999) return "10万円以下";
  return "10万円超";
}

function sanitizeFilename(value, fallback = "document") {
  const base = String(value || fallback).replace(/[\/:*?"<>|]/g, "_").replace(/\s+/g, "_").slice(0, 80);
  return base || fallback;
}

async function downloadElementAsPdf(element, filename) {
  if (!element) return false;
  if (!window.html2canvas || !window.jspdf?.jsPDF) {
    alert("PDF作成ライブラリの読み込み中です。数秒待ってからもう一度押してください。");
    return false;
  }
  const canvas = await window.html2canvas(element, {
    scale: 2,
    useCORS: true,
    backgroundColor: "#ffffff",
    scrollX: 0,
    scrollY: 0,
  });
  const pdf = new window.jspdf.jsPDF("p", "mm", "a4");
  const pageWidth = 210;
  const pageHeight = 297;
  const imgWidth = pageWidth;
  let imgHeight = (canvas.height * imgWidth) / canvas.width;
  const imgData = canvas.toDataURL("image/jpeg", 0.96);
  if (imgHeight <= pageHeight * 1.12) {
    pdf.addImage(imgData, "JPEG", 0, 0, imgWidth, pageHeight);
  } else {
    let heightLeft = imgHeight;
    let position = 0;
    pdf.addImage(imgData, "JPEG", 0, position, imgWidth, imgHeight);
    heightLeft -= pageHeight;
    while (heightLeft > 2) {
      position = heightLeft - imgHeight;
      pdf.addPage();
      pdf.addImage(imgData, "JPEG", 0, position, imgWidth, imgHeight);
      heightLeft -= pageHeight;
    }
  }
  const blob = pdf.output("blob");
  await saveBlobWithPicker(blob, filename, "application/pdf", "PDFファイル");
  return true;
}

async function saveBlobWithPicker(blob, filename, mimeType, description) {
  if (window.showSaveFilePicker) {
    const extension = filename.includes(".") ? filename.split(".").pop() : "";
    const handle = await window.showSaveFilePicker({
      suggestedName: filename,
      types: [{ description, accept: { [mimeType]: extension ? [`.${extension}`] : [] } }],
    });
    const writable = await handle.createWritable();
    await writable.write(blob);
    await writable.close();
    return true;
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  return true;
}

async function downloadElementAsWord(element, filename) {
  if (!element) return false;
  const html = `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>${filename}</title><style>
    body{font-family:"Noto Sans JP","Yu Gothic",sans-serif;color:#111827}
    table{width:100%;border-collapse:collapse;font-size:12px}
    th,td{border:1px solid #111827;padding:6px;vertical-align:top}
    .doc-title{text-align:center;font-size:24px;font-weight:700;margin-bottom:16px}
    .doc-meta{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px}
    .doc-total{text-align:right;font-size:18px;font-weight:700;margin-top:16px}
  </style></head><body>${element.outerHTML}</body></html>`;
  const blob = new Blob(["﻿", html], { type: "application/msword" });
  await saveBlobWithPicker(blob, filename, "application/msword", "Wordファイル");
  return true;
}

async function downloadDocumentAsWord(filename) {
  return downloadElementAsWord(document.querySelector(".doc-page"), filename);
}

function printElement(element) {
  if (!element) return false;
  const popup = window.open("", "_blank");
  if (!popup) {
    alert("印刷用のウィンドウを開けませんでした。ポップアップ許可を確認してください。");
    return false;
  }
  popup.document.write(`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>印刷</title><link rel="stylesheet" href="static/preview_v2.css"></head><body>${element.outerHTML}</body></html>`);
  popup.document.close();
  popup.focus();
  window.setTimeout(() => popup.print(), 300);
  return true;
}

function prepareSettlementEditableNodes(clientId) {
  const roots = Array.from(document.querySelectorAll(`[data-settlement-doc-group="${clientId}"]`));
  const nodes = roots.flatMap((root) => Array.from(root.querySelectorAll(".doc-title, .doc-meta div, .doc-table tbody td, .doc-total span, .doc-total strong, .doc-note p")));
  nodes.forEach((node) => {
    if (!node.dataset.initialHtml) node.dataset.initialHtml = node.innerHTML;
    node.classList.add("doc-editable");
    if (!node.hasAttribute("contenteditable")) node.setAttribute("contenteditable", "false");
  });
  return { roots, nodes };
}

function toggleSettlementDocumentEdit(clientId, buttonId) {
  const { roots, nodes } = prepareSettlementEditableNodes(clientId);
  if (!roots.length) return;
  const editing = !roots.some((root) => root.classList.contains("is-edit-mode"));
  roots.forEach((root) => root.classList.toggle("is-edit-mode", editing));
  nodes.forEach((node) => node.setAttribute("contenteditable", editing ? "true" : "false"));
  const button = document.getElementById(buttonId);
  if (button) button.textContent = editing ? "訂正を終了する" : "2書類を訂正する";
}

function resetSettlementDocumentEdit(clientId) {
  const { roots, nodes } = prepareSettlementEditableNodes(clientId);
  nodes.forEach((node) => {
    node.innerHTML = node.dataset.initialHtml || "";
    node.setAttribute("contenteditable", "false");
  });
  roots.forEach((root) => root.classList.remove("is-edit-mode"));
  const button = document.getElementById(`settlement-edit-${clientId}`);
  if (button) button.textContent = "2書類を訂正する";
}

function captureSettlementDocumentHtml(clientId) {
  const { roots, nodes } = prepareSettlementEditableNodes(clientId);
  nodes.forEach((node) => node.setAttribute("contenteditable", "false"));
  roots.forEach((root) => root.classList.remove("is-edit-mode"));
  const main = document.getElementById(`settlement-main-${clientId}`);
  const detail = document.getElementById(`settlement-detail-${clientId}`);
  const clean = (element) => {
    if (!element) return "";
    const clone = element.cloneNode(true);
    clone.classList.remove("is-edit-mode");
    clone.querySelectorAll("[contenteditable]").forEach((node) => node.setAttribute("contenteditable", "false"));
    return clone.outerHTML;
  };
  return {
    mainDocumentHtml: clean(main),
    detailDocumentHtml: clean(detail),
  };
}

async function downloadElementIdAsPdf(elementId) {
  const element = document.getElementById(elementId);
  if (!element) {
    alert("PDF化する書類が見つかりません。");
    return false;
  }
  const filename = element.dataset.filename || "document.pdf";
  return downloadElementAsPdf(element, filename);
}

function findClientByName(name) {
  const normalized = (name || "").trim();
  return Object.entries(previewData().clients || {}).find(([, client]) => client.name === normalized) || null;
}

function buildDocRows(items) {
  const products = previewData().products || {};
  const rows = [];
  const rowCount = Math.max(15, items.length);
  for (let index = 0; index < rowCount; index += 1) {
    const item = items[index];
    if (!item) {
      rows.push(`
        <tr>
          <td class="doc-col-no">&nbsp;</td>
          <td class="doc-col-name"></td>
          <td class="doc-col-brand"></td>
          <td class="doc-col-condition"></td>
          <td class="doc-col-qty"></td>
          <td class="doc-col-price"></td>
          <td class="doc-col-price"></td>
        </tr>
      `);
      continue;
    }
    const product = products[item.product];
    if (!product) continue;
    rows.push(`
      <tr>
        <td class="doc-col-no">${index + 1}</td>
        <td class="doc-col-name">${product.name}</td>
        <td class="doc-col-brand">${product.brand}</td>
        <td class="doc-col-condition">${product.condition}</td>
        <td class="doc-col-qty">1</td>
        <td class="doc-col-price doc-cell-price" data-amount="${item.price}">${formatYen(item.price)}</td>
        <td class="doc-col-price doc-cell-amount" data-amount="${item.price}">${formatYen(item.price)}</td>
      </tr>
    `);
  }
  return rows.join("");
}

function buildReturnGroups() {
  const assignments = { ...defaultAssignments(), ...getAssignments() };
  const groups = {};
  Object.entries(assignments).forEach(([productId, assignment]) => {
    const product = previewData().products?.[productId];
    if (!product) return;
    const current = getItemState(productId, product.status || "認証待ち");
    if (current.unavailable || current.clientReturned) return;
    const clientId = assignment.clientId || product.client;
    const service = assignment.service || product.service;
    const key = `${clientId}__${service}`;
    if (!groups[key]) {
      groups[key] = {
        key,
        client: clientId,
        service,
        items: [],
      };
    }
    groups[key].items.push({
      product: productId,
      price: Number(assignment.price) || Number(product.amount) || 0,
      source: assignment.source || "回答書類",
    });
  });

  return Object.values(groups).sort((left, right) => {
    const a = `${previewData().clients[left.client]?.name || ""}-${left.service}`;
    const b = `${previewData().clients[right.client]?.name || ""}-${right.service}`;
    return a.localeCompare(b, "ja");
  });
}

function applyStatus(selectId, badgeId, stateId, noticeId, productName, service) {
  const select = document.getElementById(selectId);
  const badge = document.getElementById(badgeId);
  const state = document.getElementById(stateId);
  if (!select || !badge || !state) return;

  const next = select.value;
  const card = select.closest("[data-product-id]");
  const productId = card ? card.dataset.productId : "";
  badge.textContent = next;
  badge.className = `pill ${statusClass(next)}`;
  state.textContent = next;
  if (productId) {
    setItemState(productId, { status: next, unavailable: false });
  }
  showInlineNotice(noticeId, `${productName}（${serviceLabel(service)}）の状態を「${next}」へ更新し、ユーザー画面のお知らせと商品状態へ反映する想定です。`);
  updateStageTabCounts();
}

function notifyUnavailable(noticeId, badgeId, stateId, cardId, productName) {
  const confirmed = window.confirm(`${productName} を受付不可にして、クライアントへ通知します。よろしいですか？`);
  if (!confirmed) return;
  const badge = document.getElementById(badgeId);
  const state = document.getElementById(stateId);
  if (badge) {
    badge.textContent = "受付不可";
    badge.className = "pill unavailable";
  }
  if (state) {
    state.textContent = "受付不可";
  }
  const card = document.getElementById(cardId);
  const productId = card ? card.dataset.productId : "";
  if (productId) {
    setItemState(productId, { status: "受付不可", unavailable: true });
  }
  showInlineNotice(noticeId, `${productName} は受付不可として、ユーザー画面のお知らせへ通知し、この確認一覧から外す想定です。`);
  updateStageTabCounts();
  if (card) {
    window.setTimeout(() => {
      card.style.display = "none";
    }, 250);
  }
}

function sendMemo(textareaId, noticeId, productId, productName) {
  const textarea = document.getElementById(textareaId);
  const message = (textarea?.value || "").trim();
  if (!message) {
    showInlineNotice(noticeId, "送信するメモを入力してください。");
    return;
  }
  setItemState(productId, { memo: message });
  showInlineNotice(noticeId, `${productName} について、ユーザー画面のお知らせへメモを送信する想定です。内容: ${message}`);
}

function registerVendorFile() {
  const fileInput = document.getElementById("vendor-file");
  const dateInput = document.getElementById("vendor-file-date");
  const serviceInput = document.getElementById("vendor-file-service");
  const titleInput = document.getElementById("vendor-file-title");
  const companyInput = document.getElementById("vendor-file-company");
  if (!fileInput) return;
  const fileName = (fileInput.value || "").split("\\").pop();
  if (!fileName) {
    showInlineNotice("vendor-file-notice", "登録する回答ファイルを選択してください。");
    return;
  }
  const dateLabel = dateInput && dateInput.value ? dateInput.value : "未設定";
  const service = serviceInput ? serviceLabel(serviceInput.value) : "業者卸販売";
  const title = (titleInput?.value || "").trim() || fileName;
  const company = (companyInput?.value || "").trim() || "業者名未設定";
  showInlineNotice("vendor-file-notice", `${dateLabel} / ${company} / ${service} の回答ファイル「${title}」を登録する想定です。`);
}

function assignClient(inputId, noticeId, productId, productName, price, source, service) {
  const input = document.getElementById(inputId);
  if (!input) return;
  const selected = findClientByName(input.value);
  if (!selected) {
    showInlineNotice(noticeId, "返送先クライアント名を一覧から選択してください。");
    return;
  }
  const [clientId, client] = selected;
  setAssignment(productId, {
    clientId,
    clientName: client.name,
    service,
    source,
    price,
  });
  showInlineNotice(noticeId, `${productName} を「${client.name}」の ${serviceLabel(service)} 返送候補へ振り分けました。4番で確認できます。`);
  updateStageTabCounts();
}

function syncVendorSelectionUI() {
  const rows = Array.from(document.querySelectorAll(".js-vendor-select-row[data-product-id]"));
  const summary = document.getElementById("vendor-selected-summary");
  const empty = document.getElementById("vendor-selection-empty");
  const service = getQueryParam("service") || "wholesale";
  const expectedStatus = service === "simultaneous" ? "出品中" : "査定中";

  rows.forEach((row) => {
    const productId = row.dataset.productId;
    const product = previewData().products?.[productId];
    const current = getItemState(productId, row.dataset.defaultStatus || product?.status || "認証待ち");
    const visible = !current.unavailable && !current.stage2Completed && row.dataset.service === service && current.status === expectedStatus;
    row.dataset.baseVisible = visible ? "true" : "false";
    row.style.display = visible ? "" : "none";
    const checkbox = row.querySelector(".vendor-check");
    if (!visible && checkbox) checkbox.checked = false;
  });

  if (empty) {
    const visibleRows = rows.filter((row) => row.style.display !== "none");
    empty.hidden = visibleRows.length > 0;
  }

  if (summary) {
    const names = Array.from(document.querySelectorAll(".vendor-check:checked"))
      .map((node) => node.dataset.summary || node.dataset.product)
      .filter(Boolean);
    summary.textContent = names.length ? names.join(" / ") : "まだ商品を選択していません";
  }
  applySearchFilter("vendor-outgoing-search", ".js-vendor-select-row", "vendor-selection-empty");
}

function prepareEstimateDraft(targetHref) {
  const service = getQueryParam("service") || "wholesale";
  let selectedProducts = Array.from(document.querySelectorAll(".vendor-check:checked"))
    .map((node) => node.dataset.productId)
    .filter(Boolean);
  if (!selectedProducts.length) {
    selectedProducts = Array.from(document.querySelectorAll(`.js-vendor-select-row[data-service="${service}"]`))
      .filter((row) => row.style.display !== "none")
      .map((row) => row.dataset.productId)
      .filter(Boolean);
  }
  if (!selectedProducts.length) {
    selectedProducts = Array.from(document.querySelectorAll(`.js-vendor-select-row[data-service="${service}"]`))
      .map((row) => row.dataset.productId)
      .filter(Boolean);
  }
  const vendorSelect = document.getElementById("vendor-target");
  let vendorName = vendorSelect ? vendorSelect.value : "";
  if (!vendorName && vendorSelect && vendorSelect.options.length > 1) {
    vendorSelect.selectedIndex = 1;
    vendorName = vendorSelect.value;
  }
  if (!selectedProducts.length) {
    showInlineNotice("vendor-draft-notice", "書類に入れる商品がありません。1番で状態を進めると、ここに表示されます。");
    return;
  }
  if (!vendorName) {
    showInlineNotice("vendor-draft-notice", "送付先業者を選択してください。");
    return;
  }
  setVendorDraft({
    vendorName,
    selectedProducts,
    service,
    documentType: document.getElementById("document-type")?.value || "",
    createdAt: new Date().toISOString(),
  });
  window.location.href = targetHref;
}

function renderVendorEstimateTemplate() {
  const rowsTarget = document.getElementById("vendor-estimate-rows");
  if (!rowsTarget) return;
  const draft = getVendorDraft();
  const vendorName = document.querySelector("[data-vendor-name]");
  const title = document.querySelector(".doc-title");
  const message = document.querySelector(".doc-message");
  if (!draft || !draft.selectedProducts?.length) {
    rowsTarget.innerHTML = buildDocRows([]);
    if (vendorName) vendorName.textContent = "取引先業者 御中";
    return;
  }
  const items = draft.selectedProducts.map((productId) => {
    const product = previewData().products?.[productId];
    return { product: productId, price: Number(product?.amount) || 0 };
  });
  rowsTarget.innerHTML = buildDocRows(items);
  if (vendorName) vendorName.textContent = `${draft.vendorName} 御中`;
  if (title) {
    title.textContent = draft.service === "auction" ? "オークション依頼書" : (draft.service === "simultaneous" ? "出品管理シート" : "見積依頼書");
  }
  if (message) {
    message.textContent = draft.service === "auction"
      ? "下記の商品について、オークション出品のご確認をお願いいたします。"
      : (draft.service === "simultaneous"
          ? "下記の商品について、同時出品の管理内容をご確認ください。"
          : "下記の商品について、見積のご確認をお願いいたします。");
  }
  const total = items.reduce((sum, item) => sum + item.price, 0);
  document.querySelectorAll("[data-total-output]").forEach((node) => {
    node.textContent = formatYen(total);
  });
}

function renderStage4Summary() {
  const container = document.getElementById("client-outgoing-groups");
  if (!container) return;
  const serviceFilter = container.dataset.serviceFilter || "all";
  const groups = buildReturnGroups().filter((group) => serviceFilter === "all" || group.service === serviceFilter);
  const empty = document.getElementById("client-outgoing-empty");
  if (!groups.length) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;
  const byClient = {};
  groups.forEach((group) => {
    if (!byClient[group.client]) byClient[group.client] = [];
    byClient[group.client].push(group);
  });
  container.innerHTML = Object.entries(byClient).map(([clientId, clientGroups]) => {
    const client = previewData().clients?.[clientId];
    const serviceCards = clientGroups.map((group) => {
      const total = group.items.reduce((sum, item) => sum + item.price, 0);
      const names = group.items.map((item) => previewData().products?.[item.product]?.name).filter(Boolean).join(" / ");
      const query = encodeURIComponent(group.key);
      return `
        <div class="compact-item">
          <strong>${serviceLabel(group.service)}</strong>
          <span>${group.items.length}点 / ${names}</span>
          <span>合計予定金額 ${formatYen(total)}</span>
          <div class="card-actions">
            <a class="btn btn-soft" href="documents_v2_client_delivery.html?group=${query}">返送内容を確認する</a>
            <a class="btn btn-primary" href="documents_v2_client_statement_template.html?group=${query}">買取明細書を作成する</a>
          </div>
        </div>
      `;
    }).join("");
    const totalCount = clientGroups.reduce((sum, group) => sum + group.items.length, 0);
    const services = clientGroups.map((group) => serviceLabel(group.service)).join(" / ");
    return `
      <div class="summary-card js-client-group js-filter-card" data-base-visible="true" data-search="${client?.name || ""} ${client?.client_no || ""} ${client?.request_id || ""} ${clientGroups.map((group) => serviceLabel(group.service)).join(" ")} ${clientGroups.map((group) => group.items.map((item) => previewData().products?.[item.product]?.name || '').join(' ')).join(' ')}">
        <div class="summary-head">
          <div>
            <div class="summary-client">${client?.name || "クライアント"}</div>
            <div class="summary-meta">${client?.client_no || client?.request_id || ""} / 返送対象 ${totalCount}点 / ${services}</div>
          </div>
          <span class="pill payment">返送準備中</span>
        </div>
        <details>
          <summary>
            <span>サービス別に確認</span>
            <span>${clientGroups.length}区分</span>
          </summary>
          <div class="compact-list service-detail-body">${serviceCards}</div>
        </details>
      </div>
    `;
  }).join("");
  applySearchFilter("client-outgoing-search", ".js-client-group", "client-outgoing-empty");
}

function getGroupFromQuery() {
  const params = new URLSearchParams(window.location.search);
  const key = params.get("group");
  if (!key) return null;
  return buildReturnGroups().find((group) => group.key === key) || null;
}

function renderClientDelivery() {
  const container = document.getElementById("client-delivery-items");
  if (!container) return;
  const group = getGroupFromQuery();
  const empty = document.getElementById("client-delivery-empty");
  const title = document.getElementById("client-delivery-title");
  const description = document.getElementById("client-delivery-description");
  const templateLink = document.getElementById("client-delivery-template-link");
  if (!group) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  const client = previewData().clients?.[group.client];
  if (title) title.textContent = `${client?.name || "クライアント"} / ${serviceLabel(group.service)} の返送内容`;
  if (description) description.textContent = `ここでどの商品を返送書類に含めるかを確認し、${serviceLabel(group.service)} 用の買取明細書へ進みます。`;
  if (templateLink) templateLink.href = `documents_v2_client_statement_template.html?group=${encodeURIComponent(group.key)}`;
  if (empty) empty.hidden = group.items.length > 0;
  container.innerHTML = group.items.map((item) => {
    const product = previewData().products?.[item.product];
    if (!product) return "";
    const detailHref = product.detail_page;
    const assignment = getAssignments()[item.product];
    return `
      <div class="product-card js-product-card" data-product-id="${item.product}" data-service="${product.service}" data-default-status="${product.status}" data-stage="client-outgoing">
        <div class="product-thumb">
          <img src="${product.image}" alt="${product.name}">
        </div>
        <div class="product-body">
          <div class="product-top">
            <div>
              <div class="product-title">${product.name}</div>
              <div class="product-meta">${serviceLabel(product.service)} / ${item.source}</div>
            </div>
            <span class="pill payment">返送予定</span>
          </div>
          <div class="detail-grid">
            <div class="field-block"><div class="field-label">売却額</div><div class="field-value">${formatYen(item.price)}</div></div>
            <div class="field-block"><div class="field-label">商品詳細</div><div class="field-value"><a href="${detailHref}">${product.name} の詳細を見る</a></div></div>
            <div class="field-block"><div class="field-label">返送先クライアント</div><div class="field-value">${assignment?.clientName || (previewData().clients?.[group.client]?.name || "")}</div></div>
            <div class="field-block"><div class="field-label">返送書類</div><div class="field-value">買取明細書へ反映</div></div>
          </div>
          <p id="delivery-note-${item.product}" class="inline-note">3番で反映された返送先です。内容を確認してから買取明細書へ進みます。</p>
        </div>
      </div>
    `;
  }).join("");
}

function renderStatementTemplate() {
  const rowsTarget = document.getElementById("client-statement-rows");
  if (!rowsTarget) return;
  const group = getGroupFromQuery();
  const clientName = document.querySelector("[data-client-name]");
  const clientService = document.querySelector("[data-client-service]");
  const backLink = document.getElementById("statement-back-link");
  if (!group) {
    rowsTarget.innerHTML = buildDocRows([]);
    return;
  }
  const client = previewData().clients?.[group.client];
  if (clientName) clientName.textContent = client?.name || "クライアント名";
  if (clientService) clientService.textContent = serviceLabel(group.service);
  if (backLink) backLink.href = `documents_v2_client_delivery.html?group=${encodeURIComponent(group.key)}`;
  const sendLink = document.getElementById("statement-send-link");
  if (sendLink) sendLink.href = `documents_v2_user_item_editor.html?group=${encodeURIComponent(group.key)}`;
  rowsTarget.innerHTML = buildDocRows(group.items);
  const total = group.items.reduce((sum, item) => sum + item.price, 0);
  document.querySelectorAll("[data-total-output]").forEach((node) => {
    node.textContent = formatYen(total);
  });
}

function completedDocKindLabel(kind) {
  if (kind === "client_estimate_request_pending") return "クライアント返送見積依頼書";
  if (kind === "client_estimate_request") return "クライアントから届いた見積依頼書";
  if (kind === "settlement_statement") return "仕切書";
  if (kind === "calculation_statement") return "計算書";
  if (kind === "vendor_outgoing") return "開花から業者への見積依頼書";
  if (kind === "client_outgoing") return "クライアント返送書類";
  return "書類";
}

function completedDocTitle(doc) {
  if (doc.kind === "client_estimate_request_pending") return "見積依頼書";
  if (doc.kind === "client_estimate_request") return "見積依頼書";
  if (doc.kind === "settlement_statement") return "仕切書";
  if (doc.kind === "calculation_statement") return "代行仕入れ計算書";
  if (doc.kind === "client_outgoing") return "買取明細書";
  if (doc.service === "auction") return "オークション依頼書";
  if (doc.service === "simultaneous") return "出品管理シート";
  return "見積依頼書";
}

function completedDocItems(doc) {
  if (Array.isArray(doc.items) && doc.items.length) return doc.items;
  return (doc.products || []).map((productId) => {
    const product = previewData().products?.[productId];
    return { product: productId, price: Number(product?.amount) || 0, source: doc.partner || "履歴書類" };
  });
}

function buildSettlementCompletedDocHtml(doc) {
  if (doc.mainDocumentHtml || doc.detailDocumentHtml) {
    return `
      <div class="settlement-history-docs">
        ${doc.mainDocumentHtml || ""}
        ${doc.detailDocumentHtml || ""}
      </div>
    `;
  }
  const supportItems = Array.isArray(doc.settlementItems) && doc.settlementItems.length
    ? doc.settlementItems
    : completedDocItems(doc).map((item) => {
        const product = previewData().products?.[item.product] || {};
        const saleAmount = Number(item.saleAmount || product.amount || item.price || 0);
        return {
          product: item.product,
          productName: product.name || item.source || "商品名未設定",
          brand: product.brand || "-",
          saleAmount,
          amount: Number(item.price || item.amount || 0),
          tierLabel: settlementSupportTierLabel(saleAmount),
          status: item.status || "完了",
        };
      });
  const supportTotal = Number(doc.supportFee || supportItems.reduce((sum, item) => sum + (Number(item.amount) || 0), 0));
  const monthlyFee = Number(doc.monthlyFee || 0);
  const monthlyFeeLabel = formatMonthlyPlanFee({
    monthlyFee,
    monthlyPlanCustom: Boolean(doc.monthlyPlanCustom),
  });
  const monthlyPlanLabel = [doc.monthlyPlan || "月額プラン", doc.monthlyPlanRange].filter(Boolean).join(" / ");
  const subtotal = monthlyFee + supportTotal;
  const detailRows = supportItems.length
    ? supportItems.map((item, index) => `
        <tr>
          <td class="statement-col-no">${index + 1}</td>
          <td>${item.productName || "商品名未設定"}</td>
          <td>${item.brand || "-"}</td>
          <td class="statement-col-price">${formatYen(item.saleAmount || 0)}</td>
          <td>${item.tierLabel || settlementSupportTierLabel(item.saleAmount)}</td>
          <td class="statement-col-price">${formatYen(item.amount || 0)}</td>
        </tr>
      `).join("")
    : `<tr><td colspan="6">対象の商品明細はありません。</td></tr>`;
  const dateLabel = doc.createdAt ? new Date(doc.createdAt).toLocaleDateString("ja-JP") : new Date().toLocaleDateString("ja-JP");
  return `
    <div class="doc-page statement-doc">
      <div class="doc-title">仕切書</div>
      <div class="doc-meta">
        <div><strong>発行日</strong> ${dateLabel}</div>
        <div><strong>宛先</strong> ${doc.partner || "クライアント"} 様</div>
        <div><strong>発行者</strong> 株式会社開花</div>
        <div><strong>対象月</strong> ${doc.month || "-"}</div>
        <div><strong>書類区分</strong> 利用明細・仕切書</div>
        <div><strong>支払方法</strong> 指定口座振込</div>
      </div>
      <table class="doc-table statement-table">
        <thead><tr><th class="statement-col-no">No.</th><th class="statement-col-desc">項目</th><th class="statement-col-qty">数量</th><th class="statement-col-price">金額</th></tr></thead>
        <tbody>
          <tr><td class="statement-col-no">1</td><td>月額利用料（${monthlyPlanLabel}）</td><td class="statement-col-qty">1式</td><td class="statement-col-price">${monthlyFeeLabel}</td></tr>
          <tr><td class="statement-col-no">2</td><td>撮影・梱包・発送代行サポート費用</td><td class="statement-col-qty">${supportItems.length}点</td><td class="statement-col-price">${formatYen(supportTotal)}</td></tr>
        </tbody>
      </table>
      <div class="doc-total"><span>合計金額</span><strong>${formatYen(subtotal)}</strong></div>
      <div class="doc-note"><strong>備考</strong><p>商品別の対象内容は、別紙「撮影・梱包・発送代行 商品明細」に記載しています。</p></div>
      <div class="statement-detail-sheet">
        <div class="doc-title">撮影・梱包・発送代行 商品明細</div>
        <table class="doc-table statement-table">
          <thead><tr><th class="statement-col-no">No.</th><th>商品名</th><th>ブランド</th><th class="statement-col-price">売上金額</th><th>料金帯</th><th class="statement-col-price">サポート費用</th></tr></thead>
          <tbody>${detailRows}</tbody>
        </table>
      </div>
    </div>
  `;
}

function buildCompletedDocHtml(doc) {
  if (doc.kind === "settlement_statement") return buildSettlementCompletedDocHtml(doc);
  const items = completedDocItems(doc);
  const total = items.reduce((sum, item) => sum + (Number(item.price) || 0), 0);
  const dateLabel = doc.createdAt ? new Date(doc.createdAt).toLocaleDateString("ja-JP") : new Date().toLocaleDateString("ja-JP");
  const recipient = doc.kind === "client_outgoing" || doc.kind === "calculation_statement"
    ? `${doc.partner || "クライアント"} 様`
    : (doc.kind === "client_estimate_request" || doc.kind === "client_estimate_request_pending")
      ? "株式会社開花 御中"
      : `${doc.partner || "取引先業者"} 御中`;
  const message = doc.kind === "calculation_statement"
    ? "下記の通り、代行仕入れに関する計算書をご案内いたします。"
    : doc.kind === "client_outgoing"
    ? "下記の通り、買取明細をご案内いたします。"
    : (doc.kind === "client_estimate_request" || doc.kind === "client_estimate_request_pending")
      ? "下記の商品について、見積依頼書として返送いたします。"
      : "下記の商品について、見積のご確認をお願いいたします。";
  const issuer = (doc.kind === "client_estimate_request" || doc.kind === "client_estimate_request_pending") ? (doc.partner || "クライアント") : "株式会社開花";
  return `
    <div class="doc-page">
      <div class="doc-title">${completedDocTitle(doc)}</div>
      <div class="doc-meta">
        <div><strong>発行日</strong> ${dateLabel}</div>
        <div><strong>宛先</strong> ${recipient}</div>
        <div><strong>発行者</strong> ${issuer}</div>
        <div><strong>対象サービス</strong> ${serviceLabel(doc.service)}</div>
      </div>
      <div class="doc-message">${message}</div>
      <table class="doc-table">
        <thead>
          <tr>
            <th class="doc-col-no">No.</th>
            <th class="doc-col-name">商品名</th>
            <th class="doc-col-brand">ブランド</th>
            <th class="doc-col-condition">状態</th>
            <th class="doc-col-qty">数量</th>
            <th class="doc-col-price">単価</th>
            <th class="doc-col-price">金額</th>
          </tr>
        </thead>
        <tbody>${buildDocRows(items)}</tbody>
      </table>
      <div class="doc-total"><span>合計金額</span><strong>${formatYen(total)}</strong></div>
      <div class="doc-note"><strong>備考</strong><p>書類履歴から再出力した控えです。</p></div>
    </div>
  `;
}

async function withTemporaryCompletedDoc(docId, callback) {
  const doc = getCompletedDocs()[docId];
  if (!doc) {
    alert("履歴書類が見つかりません。");
    return false;
  }
  const wrapper = document.createElement("div");
  wrapper.style.position = "fixed";
  wrapper.style.left = "-10000px";
  wrapper.style.top = "0";
  wrapper.style.background = "#ffffff";
  wrapper.innerHTML = buildCompletedDocHtml(doc);
  document.body.appendChild(wrapper);
  const element = doc.kind === "settlement_statement"
    ? (wrapper.querySelector(".settlement-history-docs") || wrapper.querySelector(".doc-page"))
    : wrapper.querySelector(".doc-page");
  try {
    await callback(element, doc);
  } finally {
    wrapper.remove();
  }
  return true;
}

async function downloadCompletedDocPdf(docId) {
  await withTemporaryCompletedDoc(docId, async (element, doc) => {
    await downloadElementAsPdf(element, `${sanitizeFilename(doc.title || completedDocTitle(doc))}.pdf`);
  });
}

async function downloadCompletedDocWord(docId) {
  await withTemporaryCompletedDoc(docId, async (element, doc) => {
    await downloadElementAsWord(element, `${sanitizeFilename(doc.title || completedDocTitle(doc))}.doc`);
  });
}

function printCompletedDoc(docId) {
  withTemporaryCompletedDoc(docId, async (element) => {
    printElement(element);
  });
}

function renderCompletedDocumentHistory(filter = "all") {
  const container = document.getElementById("completed-doc-history");
  if (!container) return;
  if (filter === "all" && container.dataset.completedFilter) {
    filter = container.dataset.completedFilter;
  }
  const empty = document.getElementById("completed-doc-empty");
  const docs = Object.values(getCompletedDocs())
    .filter((doc) => filter === "all" || doc.kind === filter)
    .sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")));
  if (!docs.length) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;
  container.innerHTML = docs.map((doc) => {
    const productNames = (doc.products || []).map((productId) => previewData().products?.[productId]?.name).filter(Boolean);
    const dateLabel = doc.createdAt ? new Date(doc.createdAt).toLocaleString("ja-JP") : "保存日時未設定";
    const roleId = `role-${doc.id}`;
    const noticeId = `cancel-notice-${doc.id}`;
    return `
      <div class="file-card">
        <div class="file-head">
          <div>
            <div class="file-title">${doc.title || completedDocKindLabel(doc.kind)}</div>
            <div class="file-meta">${completedDocKindLabel(doc.kind)} / ${doc.partner || "相手先未設定"} / ${serviceLabel(doc.service)} / 完了日 ${dateLabel}</div>
          </div>
          <span class="pill completed">完了</span>
        </div>
        <p class="section-note">${productNames.join(" / ") || "商品未設定"}</p>
        <div class="card-actions" style="margin-top:12px;">
          <button class="btn btn-primary btn-compact" type="button" onclick="downloadCompletedDocPdf('${doc.id}')">PDF保存</button>
          <button class="btn btn-soft btn-compact" type="button" onclick="downloadCompletedDocWord('${doc.id}')">Word保存</button>
          <button class="btn btn-outline btn-compact" type="button" onclick="printCompletedDoc('${doc.id}')">印刷</button>
        </div>
        <div class="owner-cancel-box">
          <label class="field">
            <span>取消権限</span>
            <select id="${roleId}">
              <option value="admin">管理者</option>
              <option value="owner">オーナー</option>
              <option value="staff">一般スタッフ</option>
            </select>
          </label>
          <button class="btn btn-outline btn-compact" type="button" onclick="cancelCompletedDocument('${doc.id}','${roleId}','${noticeId}')">権限者として取消</button>
          <p id="${noticeId}" class="inline-notice" hidden></p>
        </div>
      </div>
    `;
  }).join("");
}

function cancelCompletedDocument(docId, roleSelectId, noticeId) {
  const role = document.getElementById(roleSelectId)?.value || "";
  if (!["owner", "admin"].includes(role)) {
    showInlineNotice(noticeId, "取消はオーナー、または権限を付与された管理者のみ実行できる想定です。");
    return;
  }
  const confirmed = window.confirm("この完了書類を取り消し、対象商品を再度処理中へ戻します。よろしいですか？");
  if (!confirmed) return;
  const doc = removeCompletedDoc(docId);
  if (!doc) {
    showInlineNotice(noticeId, "取消対象の書類が見つかりません。");
    return;
  }
  (doc.products || []).forEach((productId) => {
    if (doc.kind === "vendor_outgoing") {
      setItemState(productId, { stage2Completed: false, vendorDocId: null });
    } else if (doc.kind === "client_outgoing") {
      setItemState(productId, { clientReturned: false, clientDocId: null, status: "入金待ち" });
    } else if (doc.kind === "calculation_statement") {
      setItemState(productId, { calculationStatementSent: false, userDocumentDelivered: false, userNotificationUnread: false });
    }
  });
  renderCalculationCurrentPage();
  renderCompletedDocumentHistory(document.querySelector(".history-tab.is-active")?.dataset.historyFilter || "all");
  updateStageTabCounts();
}

function completeCurrentDocument(kind) {
  const createdAt = new Date().toISOString();
  if (kind === "vendor") {
    const draft = getVendorDraft();
    if (!draft || !draft.selectedProducts?.length) {
      showInlineNotice("doc-save-notice", "保存する書類の商品がありません。2番の書類作成画面から商品を選んで作成してください。");
      return;
    }
    const docId = `vendor-${Date.now()}`;
    const doc = {
      id: docId,
      kind: "vendor_outgoing",
      title: `${draft.documentType || "見積依頼書"} / ${draft.vendorName || "送付先未設定"}`,
      partner: draft.vendorName || "送付先未設定",
      service: draft.service || "wholesale",
      products: draft.selectedProducts,
      items: draft.selectedProducts.map((productId) => {
        const product = previewData().products?.[productId];
        return { product: productId, price: Number(product?.amount) || 0, source: draft.vendorName || "業者依頼書" };
      }),
      createdAt,
      status: "完了",
    };
    setCompletedDoc(doc);
    doc.products.forEach((productId) => {
      setItemState(productId, { stage2Completed: true, vendorDocId: docId });
    });
    showInlineNotice("doc-save-notice", "この業者向け書類を完了にしました。2番の進行中一覧から外れ、書類履歴へ保存されます。取消は履歴側で権限者のみ行う想定です。");
    updateStageTabCounts();
    return;
  }

  if (kind === "client") {
    const group = getGroupFromQuery();
    if (!group || !group.items?.length) {
      showInlineNotice("doc-save-notice", "保存する返送書類の商品がありません。4番の返送候補から作成してください。");
      return;
    }
    const client = previewData().clients?.[group.client];
    const docId = `client-${Date.now()}`;
    const doc = {
      id: docId,
      kind: "client_outgoing",
      title: `買取明細書 / ${client?.name || "クライアント"} / ${serviceLabel(group.service)}`,
      partner: client?.name || "クライアント",
      clientId: group.client,
      service: group.service,
      products: group.items.map((item) => item.product),
      items: group.items,
      createdAt,
      status: "完了",
    };
    setCompletedDoc(doc);
    group.items.forEach((item) => {
      setItemState(item.product, {
        clientReturned: true,
        clientDocId: docId,
        status: "完了",
        userDocumentDelivered: true,
        userNotificationUnread: true,
        userNotificationTitle: "買取明細書が届きました",
      });
    });
    showInlineNotice("doc-save-notice", "このクライアント返送書類を完了にしました。4番の返送候補から外れ、書類履歴へ保存されます。取消は履歴側で権限者のみ行う想定です。");
    updateStageTabCounts();
  }
}

async function saveCurrentDocumentAsPdf(kind, filename) {
  const element = document.querySelector(".doc-page");
  try {
    const ok = await downloadElementAsPdf(element, filename);
    if (ok) {
      completeCurrentDocument(kind);
    }
  } catch (error) {
    if (error?.name !== "AbortError") {
      alert("PDF保存に失敗しました。もう一度お試しください。");
    }
  }
}

async function saveCurrentDocumentAsWord(kind, filename) {
  try {
    const ok = await downloadElementAsWord(document.querySelector(".doc-page"), filename);
    if (ok) {
      completeCurrentDocument(kind);
    }
  } catch (error) {
    if (error?.name !== "AbortError") {
      alert("Word保存に失敗しました。もう一度お試しください。");
    }
  }
}

function assignDeliveryProduct(inputId, noticeId, productId, productName, price, source, service) {
  const input = document.getElementById(inputId);
  if (!input) return;
  const selected = findClientByName(input.value);
  if (!selected) {
    showInlineNotice(noticeId, "返送先クライアント名を一覧から選択してください。");
    return;
  }
  const [clientId, client] = selected;
  setAssignment(productId, {
    clientId,
    clientName: client.name,
    service,
    source,
    price,
  });
  showInlineNotice(noticeId, `${productName} を「${client.name}」の ${serviceLabel(service)} 返送書類へ振り分けました。`);
  updateStageTabCounts();
}

function sendClientEstimateRequestFromUser(noticeId) {
  const group = getGroupFromQuery();
  if (!group || !group.items?.length) return;
  const client = previewData().clients?.[group.client];
  const docId = `client-estimate-${group.key}`;
  const doc = {
    id: docId,
    kind: "client_estimate_request_pending",
    title: `見積依頼書 / ${client?.name || "クライアント"} / ${serviceLabel(group.service)}`,
    partner: client?.name || "クライアント",
    clientId: group.client,
    service: group.service,
    products: group.items.map((item) => item.product),
    items: group.items,
    createdAt: new Date().toISOString(),
    status: "認証待ち",
  };
  setCompletedDoc(doc);
  group.items.forEach((item) => {
    setItemState(item.product, {
      estimateRequestReturned: true,
      userEstimateRequestSent: true,
      userNotificationUnread: false,
    });
  });
  showInlineNotice(noticeId, "ユーザーから見積依頼書として返送され、管理側の書類一覧「クライアント返送見積依頼書」に反映されました。管理側で認証すると履歴へ移動します。");
  renderUserDocumentsPreview();
  renderPendingClientEstimateRequests();
}

function approveClientEstimateRequest(docId, noticeId) {
  const state = loadPreviewState();
  const doc = state.completedDocs?.[docId];
  if (!doc) {
    showInlineNotice(noticeId, "対象の見積依頼書が見つかりません。");
    return;
  }
  doc.kind = "client_estimate_request";
  doc.status = "認証済み";
  doc.approvedAt = new Date().toISOString();
  state.completedDocs[docId] = doc;
  savePreviewState(state);
  showInlineNotice(noticeId, "見積依頼書を認証しました。書類一覧から外れ、書類履歴の「クライアント返送見積依頼書」へ保存されます。");
  renderPendingClientEstimateRequests();
  renderCompletedDocumentHistory("client_estimate_request");
}

function renderPendingClientEstimateRequests() {
  const container = document.getElementById("pending-client-estimate-list");
  if (!container) return;
  const empty = document.getElementById("pending-client-estimate-empty");
  const docs = Object.values(getCompletedDocs())
    .filter((doc) => doc.kind === "client_estimate_request_pending")
    .sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")));
  if (!docs.length) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;
  container.innerHTML = docs.map((doc) => {
    const productNames = completedDocItems(doc).map((item) => previewData().products?.[item.product]?.name).filter(Boolean);
    const dateLabel = doc.createdAt ? new Date(doc.createdAt).toLocaleString("ja-JP") : "返送日時未設定";
    const noticeId = `approve-estimate-${doc.id}`;
    return `
      <div class="file-card">
        <div class="file-head">
          <div>
            <div class="file-title">${doc.title || "見積依頼書"}</div>
            <div class="file-meta">${doc.partner || "クライアント"} / ${serviceLabel(doc.service)} / 返送日時 ${dateLabel}</div>
          </div>
          <span class="pill pending">認証待ち</span>
        </div>
        <p class="section-note">${productNames.join(" / ") || "商品未設定"}</p>
        <div class="card-actions" style="margin-top:12px;">
          <button class="btn btn-primary btn-compact" type="button" onclick="approveClientEstimateRequest('${doc.id}','${noticeId}')">認証して履歴へ移動</button>
          <button class="btn btn-soft btn-compact" type="button" onclick="downloadCompletedDocPdf('${doc.id}')">PDF確認</button>
          <button class="btn btn-outline btn-compact" type="button" onclick="printCompletedDoc('${doc.id}')">印刷</button>
        </div>
        <p id="${noticeId}" class="inline-notice" hidden></p>
      </div>
    `;
  }).join("");
}

function settlementDataForClient(clientId, month) {
  const client = previewData().clients?.[clientId];
  if (!client) return null;
  const products = Object.entries(previewData().products || {}).filter(([, product]) => product.client === clientId);
  const completedProducts = products.filter(([productId, product]) => {
    const current = getItemState(productId, product.status || "認証待ち");
    return current.clientReturned || ["販売済み", "完了"].includes(current.status);
  });
  const planInfo = client.monthly_plan
    ? {
        plan: client.monthly_plan,
        fee: Number(client.monthly_fee || 0),
        range: client.monthly_plan_range || "",
        itemCount: Number(client.monthly_plan_item_count || products.length),
        isCustom: Boolean(client.monthly_plan_custom),
      }
    : monthlyPlanFromItemCount(products.length);
  const monthlyFee = Number(planInfo.fee || 0);
  const supportItems = completedProducts.map(([productId, product]) => {
    const current = getItemState(productId, product.status || "認証待ち");
    const saleAmount = Number(current.soldPrice || product.amount || current.amount || 0);
    const amount = settlementSupportFeeFromSale(saleAmount);
    const shortName = shortProductName(product.name);
    return {
      productId,
      product,
      productName: product.name,
      shortName,
      saleAmount,
      amount,
      label: `撮影・梱包・発送代行サポート費用（${shortName}）`,
      tierLabel: settlementSupportTierLabel(saleAmount),
      status: current.status || product.status || "完了",
      note: current.settlementNote || "",
    };
  });
  const supportFee = supportItems.reduce((sum, item) => sum + item.amount, 0);
  return {
    client,
    clientId,
    month,
    monthlyPlan: planInfo.plan,
    monthlyPlanRange: planInfo.range,
    monthlyPlanItemCount: planInfo.itemCount,
    monthlyPlanCustom: planInfo.isCustom,
    products,
    completedProducts,
    monthlyFee,
    supportItems,
    supportFee,
    total: monthlyFee + supportFee,
  };
}

function createSettlementDocument(clientId, month, renderedDocs = null) {
  const data = settlementDataForClient(clientId, month);
  if (!data) return null;
  const docId = `settlement-${clientId}-${month}`;
  return setCompletedDoc({
    id: docId,
    kind: "settlement_statement",
    title: `仕切書 / ${data.client.name} / ${month}`,
    partner: data.client.name,
    clientId,
    service: "wholesale",
    products: data.supportItems.map((item) => item.productId),
    items: data.supportItems.map((item) => ({ product: item.productId, price: item.amount, source: item.label, saleAmount: item.saleAmount, status: item.status })),
    settlementItems: data.supportItems.map((item) => ({
      product: item.productId,
      productName: item.productName,
      shortName: item.shortName,
      brand: item.product.brand,
      saleAmount: item.saleAmount,
      amount: item.amount,
      tierLabel: item.tierLabel,
      status: item.status,
      note: item.note,
    })),
    createdAt: new Date().toISOString(),
    status: "送付済み",
    month,
    monthlyPlan: data.monthlyPlan,
    monthlyPlanRange: data.monthlyPlanRange,
    monthlyPlanItemCount: data.monthlyPlanItemCount,
    monthlyPlanCustom: data.monthlyPlanCustom,
    monthlyFee: data.monthlyFee,
    supportFee: data.supportFee,
    total: data.total,
    mainDocumentHtml: renderedDocs?.mainDocumentHtml || "",
    detailDocumentHtml: renderedDocs?.detailDocumentHtml || "",
  });
}

function sendSettlementStatement(clientId, noticeId) {
  const month = document.getElementById("settlement-month-filter")?.value || "2026-04";
  const data = settlementDataForClient(clientId, month);
  if (!data) return;
  const firstConfirm = window.confirm(`${data.client.name} 様の「仕切書」と「商品明細」の2書類を確認しましたか？`);
  if (!firstConfirm) return;
  const secondConfirm = window.confirm(`最終確認です。${data.client.name} 様へ ${month} の仕切書・商品明細を送付して、履歴へ保存します。よろしいですか？`);
  if (!secondConfirm) return;
  const renderedDocs = captureSettlementDocumentHtml(clientId);
  const doc = createSettlementDocument(clientId, month, renderedDocs);
  if (!doc) return;
  showInlineNotice(noticeId, `${doc.partner} の ${month} 仕切書と商品明細を送付済みにしました。訂正後の内容も履歴の「仕切書」へ保存されます。`);
  renderCompletedDocumentHistory("settlement_statement");
}

function sendAllSettlementStatements(noticeId) {
  const month = document.getElementById("settlement-month-filter")?.value || "2026-04";
  const clients = Object.keys(previewData().clients || {});
  const firstConfirm = window.confirm(`${month} の仕切書・商品明細を全クライアント ${clients.length} 名分まとめて送付します。各書類の確認は完了していますか？`);
  if (!firstConfirm) return;
  const secondConfirm = window.confirm(`最終確認です。全クライアントへ2書類ずつ送付して履歴へ保存します。よろしいですか？`);
  if (!secondConfirm) return;
  clients.forEach((clientId) => createSettlementDocument(clientId, month, captureSettlementDocumentHtml(clientId)));
  showInlineNotice(noticeId, `${month} の仕切書と商品明細を全クライアント ${clients.length} 名分、一括送付済みにしました。履歴の「仕切書」へ保存されます。`);
  renderSettlementCurrentPage();
  renderCompletedDocumentHistory("settlement_statement");
}

function renderSettlementCurrentPage() {
  const container = document.getElementById("settlement-current-list");
  if (!container) return;
  const month = document.getElementById("settlement-month-filter")?.value || "2026-04";
  container.innerHTML = Object.entries(previewData().clients || {}).map(([clientId, client]) => {
    const data = settlementDataForClient(clientId, month);
    if (!data) return "";
    const noticeId = `settlement-notice-${clientId}`;
    const mainDocId = `settlement-main-${clientId}`;
    const detailDocId = `settlement-detail-${clientId}`;
    const editButtonId = `settlement-edit-${clientId}`;
    const fileBase = sanitizeFilename(`${client.name}_${month}`);
    const monthlyFeeLabel = formatMonthlyPlanFee(data);
    const monthlyPlanLabel = [data.monthlyPlan, data.monthlyPlanRange].filter(Boolean).join(" / ");
    const statementRows = [
      `<tr><td class="statement-col-no">1</td><td>月額利用料（${monthlyPlanLabel}）</td><td class="statement-col-qty">1式</td><td class="statement-col-price">${monthlyFeeLabel}</td></tr>`,
      `<tr><td class="statement-col-no">2</td><td>撮影・梱包・発送代行サポート費用</td><td class="statement-col-qty">${data.supportItems.length}点</td><td class="statement-col-price">${formatYen(data.supportFee)}</td></tr>`,
    ].join("");
    const detailRows = data.supportItems.length ? data.supportItems.map((item, index) => `
      <tr>
        <td class="statement-col-no">${index + 1}</td>
        <td>${item.product.name}</td>
        <td>${item.product.brand || "-"}</td>
        <td class="statement-col-price">${formatYen(item.saleAmount)}</td>
        <td>${item.tierLabel}</td>
        <td class="statement-col-price">${formatYen(item.amount)}</td>
      </tr>
    `).join("") : `<tr><td colspan="6">当月の商品別サポート費用はありません。</td></tr>`;
    const productOverview = data.supportItems.length ? data.supportItems.map((item) => `
      <div class="settlement-product-line">
        <div>
          <strong>${item.product.name}</strong>
          <span>${item.product.brand || "-"} / ${item.product.category || "商品"} / ${item.status}</span>
        </div>
        <span>${formatYen(item.saleAmount)}</span>
      </div>
    `).join("") : `<div class="compact-item"><strong>当月の対象商品なし</strong><span>取引完了した商品が発生したらここに表示されます。</span></div>`;
    return `
      <details class="file-card settlement-client-card">
        <summary>
          <div>
            <div class="file-head">
              <div>
                <div class="file-title">${client.name} / ${month} 仕切書</div>
                <div class="file-meta">クライアント番号 ${client.client_no} / 登録商品 ${data.products.length}点 / 取引完了 ${data.completedProducts.length}点</div>
              </div>
              <span class="pill payment">月締め前</span>
            </div>
            <div class="settlement-client-summary-grid">
              <div class="field-block"><div class="field-label">月額利用料</div><div class="field-value">${monthlyFeeLabel}</div></div>
              <div class="field-block"><div class="field-label">撮影・梱包・発送代行</div><div class="field-value">${data.supportItems.length}点 / ${formatYen(data.supportFee)}</div></div>
              <div class="field-block"><div class="field-label">請求合計</div><div class="field-value">${formatYen(data.total)}</div></div>
            </div>
          </div>
          <span class="btn btn-soft btn-compact">詳細を確認</span>
        </summary>
        <div class="settlement-client-detail">
          <div class="settlement-detail-overview">
            <div class="field-block"><div class="field-label">月額プラン</div><div class="field-value">${monthlyPlanLabel} / ${monthlyFeeLabel}</div></div>
            <div class="field-block"><div class="field-label">登録商品数</div><div class="field-value">${data.monthlyPlanItemCount}点（判定対象）</div></div>
            <div class="field-block"><div class="field-label">仕切対象商品</div><div class="field-value">${data.supportItems.length}点 / ${formatYen(data.supportFee)}</div></div>
          </div>
          <div class="mini-panel">
            <div class="field-label">今回仕入れ・取引した商品</div>
            <div class="settlement-product-list" style="margin-top:8px;">${productOverview}</div>
          </div>
          <div class="card-actions settlement-doc-actions">
            <button id="${editButtonId}" class="btn btn-soft btn-compact" type="button" onclick="toggleSettlementDocumentEdit('${clientId}','${editButtonId}')">2書類を訂正する</button>
            <button class="btn btn-outline btn-compact" type="button" onclick="resetSettlementDocumentEdit('${clientId}')">訂正を元に戻す</button>
            <button class="btn btn-primary btn-compact" type="button" onclick="sendSettlementStatement('${clientId}','${noticeId}')">2書類を確認して送付する</button>
          </div>
          <details class="document-toggle">
            <summary><span>1. 仕切書</span><span class="btn btn-soft btn-compact">仕切書を開く</span></summary>
            <div class="card-actions settlement-print-actions">
              <button class="btn btn-primary btn-compact" type="button" onclick="downloadElementIdAsPdf('${mainDocId}')">仕切書PDF保存</button>
            </div>
            <div class="document-scroll">
              <div id="${mainDocId}" class="doc-page statement-doc settlement-doc-page" data-settlement-doc-group="${clientId}" data-filename="${fileBase}_仕切書.pdf">
              <div class="doc-title">仕切書</div>
              <div class="doc-meta">
                <div><strong>対象月</strong> ${month}</div>
                <div><strong>宛先</strong> ${client.name} 様</div>
                <div><strong>発行者</strong> 株式会社開花</div>
                <div><strong>クライアント番号</strong> ${client.client_no}</div>
                <div><strong>書類区分</strong> 利用明細・仕切書</div>
                <div><strong>支払方法</strong> 指定口座振込</div>
              </div>
              <table class="doc-table statement-table">
                <thead><tr><th class="statement-col-no">No.</th><th class="statement-col-desc">項目</th><th class="statement-col-qty">数量</th><th class="statement-col-price">金額</th></tr></thead>
                <tbody>${statementRows}</tbody>
              </table>
              <div class="doc-total"><span>合計金額</span><strong>${formatYen(data.total)}</strong></div>
              <div class="doc-note"><strong>備考</strong><p>撮影・梱包・発送代行の対象商品は、別紙「商品明細」に記載しています。</p></div>
              </div>
            </div>
          </details>
          <details class="document-toggle">
            <summary><span>2. 商品明細</span><span class="btn btn-soft btn-compact">商品明細を開く</span></summary>
            <div class="card-actions settlement-print-actions">
              <button class="btn btn-soft btn-compact" type="button" onclick="downloadElementIdAsPdf('${detailDocId}')">商品明細PDF保存</button>
            </div>
            <div class="document-scroll">
              <div id="${detailDocId}" class="doc-page statement-doc settlement-doc-page" data-settlement-doc-group="${clientId}" data-filename="${fileBase}_撮影梱包発送商品明細.pdf">
              <div class="doc-title">撮影・梱包・発送代行 商品明細</div>
              <div class="doc-meta">
                <div><strong>対象月</strong> ${month}</div>
                <div><strong>宛先</strong> ${client.name} 様</div>
                <div><strong>対象件数</strong> ${data.supportItems.length}点</div>
                <div><strong>サポート費用合計</strong> ${formatYen(data.supportFee)}</div>
                <div><strong>発行者</strong> 株式会社開花</div>
                <div><strong>書類区分</strong> 商品別明細</div>
              </div>
              <table class="doc-table statement-table">
                <thead><tr><th class="statement-col-no">No.</th><th>商品名</th><th>ブランド</th><th class="statement-col-price">売上金額</th><th>料金帯</th><th class="statement-col-price">サポート費用</th></tr></thead>
                <tbody>${detailRows}</tbody>
              </table>
              <div class="doc-total"><span>商品別サポート費用合計</span><strong>${formatYen(data.supportFee)}</strong></div>
              </div>
            </div>
          </details>
          <p id="${noticeId}" class="inline-notice" hidden></p>
        </div>
      </details>
    `;
  }).join("");
}

function calculationStatementGroups() {
  const groups = {};
  Object.entries(previewData().products || {}).forEach(([productId, product]) => {
    if (!(product.proxyPurchase || product.service === "proxy_purchase")) return;
    const current = getItemState(productId, product.status || "認証待ち");
    if (current.calculationStatementSent) return;
    const clientId = product.client;
    const client = previewData().clients?.[clientId];
    if (!client) return;
    if (!groups[clientId]) {
      groups[clientId] = {
        clientId,
        client,
        items: [],
      };
    }
    groups[clientId].items.push({
      product: productId,
      price: Number(current.proxyPurchaseAmount || product.purchasePrice || product.amount || 0),
      source: "代行仕入れ",
      status: current.status || product.status || "計算書候補",
    });
  });
  return groups;
}

function buildCalculationStatementDoc(group, docId) {
  const total = group.items.reduce((sum, item) => sum + Number(item.price || 0), 0);
  const dateLabel = new Date().toLocaleDateString("ja-JP");
  const rows = buildDocRows(group.items);
  return `
    <div id="${docId}" class="doc-page" data-filename="${sanitizeFilename(group.client.name)}_代行仕入れ計算書.pdf">
      <div class="doc-title">代行仕入れ計算書</div>
      <div class="doc-meta">
        <div><strong>発行日</strong> ${dateLabel}</div>
        <div><strong>宛先</strong> ${group.client.name} 様</div>
        <div><strong>クライアント番号</strong> ${group.client.client_no || "-"}</div>
        <div><strong>発行者</strong> 株式会社開花</div>
        <div><strong>書類区分</strong> 代行仕入れ計算書</div>
        <div><strong>対象件数</strong> ${group.items.length}点</div>
      </div>
      <div class="doc-message">下記の通り、代行仕入れに関する計算内容をご案内いたします。</div>
      <table class="doc-table">
        <thead>
          <tr>
            <th class="doc-col-no">No.</th>
            <th class="doc-col-name">商品名</th>
            <th class="doc-col-brand">ブランド</th>
            <th class="doc-col-condition">状態</th>
            <th class="doc-col-qty">数量</th>
            <th class="doc-col-price">単価</th>
            <th class="doc-col-price">金額</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="doc-total"><span>代行仕入れ計算額</span><strong>${formatYen(total)}</strong></div>
      <div class="doc-note"><strong>備考</strong><p>必要に応じて、代行仕入れに関する手数料・送料・調整額を確認してから送付してください。</p></div>
    </div>
  `;
}

function sendCalculationStatement(clientId, noticeId) {
  const group = calculationStatementGroups()[clientId];
  if (!group || !group.items.length) {
    showInlineNotice(noticeId, "送付対象の代行仕入れ商品がありません。");
    return;
  }
  const confirmed = window.confirm(`${group.client.name} 様へ代行仕入れ計算書を送付し、書類履歴へ保存します。よろしいですか？`);
  if (!confirmed) return;
  const docId = `calculation-${clientId}-${Date.now()}`;
  const doc = {
    id: docId,
    kind: "calculation_statement",
    title: `代行仕入れ計算書 / ${group.client.name}`,
    partner: group.client.name,
    clientId,
    service: "proxy_purchase",
    products: group.items.map((item) => item.product),
    items: group.items,
    createdAt: new Date().toISOString(),
    status: "送付済み",
  };
  setCompletedDoc(doc);
  group.items.forEach((item) => {
    setItemState(item.product, {
      calculationStatementSent: true,
      userDocumentDelivered: true,
      userNotificationUnread: true,
      userNotificationTitle: "代行仕入れ計算書が届きました",
    });
  });
  showInlineNotice(noticeId, `${group.client.name} 様へ代行仕入れ計算書を送付しました。商品一覧側の送付対象から外れ、下の送付済み計算書履歴と書類履歴の「計算書」へ保存されます。`);
  renderCalculationCurrentPage();
  renderCompletedDocumentHistory("calculation_statement");
}

function renderCalculationCurrentPage() {
  const container = document.getElementById("calculation-current-list");
  if (!container) return;
  const empty = document.getElementById("calculation-current-empty");
  const groups = Object.values(calculationStatementGroups());
  if (!groups.length) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;
  container.innerHTML = groups.map((group) => {
    const total = group.items.reduce((sum, item) => sum + Number(item.price || 0), 0);
    const noticeId = `calculation-notice-${group.clientId}`;
    const docId = `calculation-doc-${group.clientId}`;
    const itemRows = group.items.map((item) => {
      const product = previewData().products?.[item.product];
      return `
        <div class="settlement-product-line">
          <div>
            <strong>${product?.name || "商品名未設定"}</strong>
            <span>${product?.brand || "-"} / ${product?.category || "商品"} / ${item.status}</span>
          </div>
          <span>${formatYen(item.price)}</span>
        </div>
      `;
    }).join("");
    return `
      <details class="file-card settlement-client-card">
        <summary>
          <div>
            <div class="file-head">
              <div>
                <div class="file-title">${group.client.name} / 代行仕入れ計算書</div>
                <div class="file-meta">クライアント番号 ${group.client.client_no || "-"} / 対象 ${group.items.length}点 / 合計 ${formatYen(total)}</div>
              </div>
              <span class="pill payment">書類送付待ち</span>
            </div>
          </div>
          <span class="btn btn-soft btn-compact">送付内容を確認</span>
        </summary>
        <div class="settlement-client-detail">
          <div class="settlement-detail-overview">
            <div class="field-block"><div class="field-label">書類区分</div><div class="field-value">代行仕入れ計算書</div></div>
            <div class="field-block"><div class="field-label">対象商品</div><div class="field-value">${group.items.length}点</div></div>
            <div class="field-block"><div class="field-label">計算額</div><div class="field-value">${formatYen(total)}</div></div>
          </div>
          <div class="mini-panel">
            <div class="field-label">送付対象の商品</div>
            <div class="settlement-product-list" style="margin-top:8px;">${itemRows}</div>
          </div>
          <div class="card-actions settlement-doc-actions">
            <button class="btn btn-primary btn-compact" type="button" onclick="sendCalculationStatement('${group.clientId}','${noticeId}')">計算書を送付する</button>
            <button class="btn btn-soft btn-compact" type="button" onclick="downloadElementIdAsPdf('${docId}')">PDF保存</button>
          </div>
          <details class="document-toggle">
            <summary><span>計算書テンプレート</span><span class="btn btn-soft btn-compact">書類を開く</span></summary>
            <div class="document-scroll">
              ${buildCalculationStatementDoc(group, docId)}
            </div>
          </details>
          <p id="${noticeId}" class="inline-notice" hidden></p>
        </div>
      </details>
    `;
  }).join("");
}

function assignCurrentGroupToClient() {
  const group = getGroupFromQuery();
  if (!group) return;
  const client = previewData().clients?.[group.client];
  if (!client) return;
  group.items.forEach((item) => {
    setAssignment(item.product, {
      clientId: group.client,
      clientName: client.name,
      service: group.service,
      source: item.source,
      price: item.price,
    });
  });
  showInlineNotice("client-delivery-notice", `${client.name} の ${serviceLabel(group.service)} 商品を一括で振り分けました。`);
  renderStage4Summary();
}

function renderUserItemEditor() {
  const container = document.getElementById("user-item-editor-items");
  if (!container) return;
  const group = getGroupFromQuery();
  const empty = document.getElementById("user-item-editor-empty");
  const title = document.getElementById("user-item-editor-title");
  const copy = document.getElementById("user-item-editor-copy");
  const backLink = document.getElementById("user-item-back-link");
  const reflectionPanel = document.getElementById("user-reflection-panel");
  if (!group) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  const client = previewData().clients?.[group.client];
  if (title) title.textContent = `${client?.name || "クライアント"} の商品状態を編集する`;
  if (copy) copy.textContent = `${serviceLabel(group.service)} の返送書類を送付した後、このクライアントの商品状態を編集する想定です。`;
  if (backLink) backLink.href = `documents_v2_client_statement_template.html?group=${encodeURIComponent(group.key)}`;
  if (empty) empty.hidden = group.items.length > 0;
  if (reflectionPanel) {
    const unreadCount = group.items.filter((item) => {
      const product = previewData().products?.[item.product];
      const current = getItemState(item.product, product?.status || "認証待ち");
      return current.userNotificationUnread || current.clientReturned;
    }).length;
    reflectionPanel.hidden = false;
    reflectionPanel.innerHTML = `
      <div class="section-head">
        <div>
          <h3>ユーザー画面への反映確認</h3>
          <p class="section-note">買取明細書を送付すると、ユーザー側の書類一覧に反映され、商品ページにも送付済み・完了状態が反映されます。</p>
        </div>
        <span class="notification-badge">通知 ${unreadCount || 1}</span>
      </div>
      <div class="summary-card-grid">
        <div class="field-block"><div class="field-label">ユーザー書類</div><div class="field-value">買取明細書が書類一覧に届く想定</div></div>
        <div class="field-block"><div class="field-label">商品ページ</div><div class="field-value">対象商品に送付済み・完了状態を反映</div></div>
        <div class="field-block"><div class="field-label">通知表示</div><div class="field-value">お知らせに未読 ${unreadCount || 1} 件として表示</div></div>
        <div class="field-block"><div class="field-label">次の作業</div><div class="field-value">販売金額・送料・管理メモを確認して更新</div></div>
      </div>
      <div class="card-actions" style="margin-top:14px;">
        <a class="btn btn-soft btn-compact" href="documents_v2_user_documents.html?group=${encodeURIComponent(group.key)}">ユーザー画面の反映を見る</a>
      </div>
    `;
  }
  container.innerHTML = group.items.map((item) => {
    const product = previewData().products?.[item.product];
    if (!product) return "";
    const current = getItemState(item.product, product.status || "認証待ち");
    const selectId = `user-item-status-${item.product}`;
    const soldId = `user-item-sold-${item.product}`;
    const shippingId = `user-item-shipping-${item.product}`;
    const noteId = `user-item-note-${item.product}`;
    const noticeId = `user-item-notice-${item.product}`;
    const options = ["入金待ち", "販売済み", "完了"].map((label) => `<option value="${label}" ${current.status === label ? "selected" : ""}>${label}</option>`).join("");
    return `
      <div class="product-card">
        <div class="product-thumb"><img src="${product.image}" alt="${product.name}"></div>
        <div class="product-body">
          <div class="product-top">
            <div>
              <div class="product-title">${product.name}</div>
              <div class="product-meta">${client?.name || ""} / ${serviceLabel(group.service)} / ${formatYen(item.price)}</div>
            </div>
            <span class="pill ${statusClass(current.status)}">${current.status}</span>
          </div>
          <div class="status-row status-row-actions" style="margin-top:12px;">
            <select id="${selectId}" class="status-select">${options}</select>
            <button class="btn btn-primary" type="button" onclick="updateUserItemStatus('${item.product}','${selectId}','${soldId}','${shippingId}','${noteId}','${noticeId}')">ユーザー商品を更新する</button>
          </div>
          <div class="user-edit-grid">
            <label class="field">
              <span>販売金額</span>
              <input id="${soldId}" type="number" min="0" step="1" value="${current.soldPrice || item.price || 0}" placeholder="売却額を入力">
            </label>
            <label class="field">
              <span>送料</span>
              <input id="${shippingId}" type="number" min="0" step="1" value="${current.shipping || 0}" placeholder="送料を入力">
            </label>
            <label class="field field-wide">
              <span>管理メモ</span>
              <textarea id="${noteId}" rows="3" placeholder="販売済み・入金・発送メモなどを入力">${current.settlementNote || ""}</textarea>
            </label>
          </div>
          <p id="${noticeId}" class="inline-notice" hidden></p>
        </div>
      </div>
    `;
  }).join("");
}

function renderUserDocumentsPreview() {
  const list = document.getElementById("user-documents-list");
  const productList = document.getElementById("user-documents-products");
  if (!list || !productList) return;
  const group = getGroupFromQuery();
  const empty = document.getElementById("user-documents-empty");
  const title = document.getElementById("user-documents-title");
  const copy = document.getElementById("user-documents-copy");
  const badge = document.getElementById("user-documents-notification");
  if (!group) {
    list.innerHTML = "";
    productList.innerHTML = "";
    if (empty) empty.hidden = false;
    if (badge) badge.textContent = "通知 0";
    return;
  }
  const client = previewData().clients?.[group.client];
  if (title) title.textContent = `${client?.name || "クライアント"} のユーザー画面`;
  if (copy) copy.textContent = `${serviceLabel(group.service)} の買取明細書送付後に、書類一覧・通知・商品ページへ反映される内容です。`;
  const docs = Object.values(getCompletedDocs())
    .filter((doc) => doc.kind === "client_outgoing" && doc.clientId === group.client && doc.service === group.service)
    .sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")));
  const estimateDocs = Object.values(getCompletedDocs())
    .filter((doc) => ["client_estimate_request_pending", "client_estimate_request"].includes(doc.kind) && doc.clientId === group.client && doc.service === group.service)
    .sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")));
  const estimateSent = estimateDocs.length > 0;
  const deliveredItems = group.items.filter((item) => {
    const product = previewData().products?.[item.product];
    const current = getItemState(item.product, product?.status || "認証待ち");
    return current.userDocumentDelivered || current.clientReturned;
  });
  const unreadCount = group.items.filter((item) => {
    const product = previewData().products?.[item.product];
    const current = getItemState(item.product, product?.status || "認証待ち");
    return current.userNotificationUnread || current.clientReturned;
  }).length;
  if (badge) badge.textContent = `通知 ${unreadCount}`;
  if (!docs.length && !deliveredItems.length) {
    list.innerHTML = "";
    if (empty) empty.hidden = false;
  } else {
    if (empty) empty.hidden = true;
    const fallbackCard = deliveredItems.length ? `
      <div class="file-card">
        <div class="file-head">
          <div>
            <div class="file-title">買取明細書 / ${client?.name || "クライアント"} / ${serviceLabel(group.service)}</div>
            <div class="file-meta">ユーザー書類一覧へ反映済み / 未読通知 ${unreadCount} 件</div>
          </div>
          <span class="pill completed">届いた書類</span>
        </div>
        <div class="card-actions" style="margin-top:12px;">
          <button class="btn btn-primary btn-compact" type="button" onclick="sendClientEstimateRequestFromUser('user-estimate-request-notice')">${estimateSent ? "見積依頼書を再送する" : "見積依頼書として返送する"}</button>
          <span class="pill ${estimateSent ? "completed" : "pending"}">${estimateSent ? "見積依頼書 返送済み" : "返送待ち"}</span>
        </div>
        <p id="user-estimate-request-notice" class="inline-notice" hidden></p>
      </div>
    ` : "";
    list.innerHTML = docs.length ? docs.map((doc) => {
      const dateLabel = doc.createdAt ? new Date(doc.createdAt).toLocaleString("ja-JP") : "送付日時未設定";
      return `
        <div class="file-card">
          <div class="file-head">
            <div>
              <div class="file-title">${doc.title || "買取明細書"}</div>
              <div class="file-meta">送付日時 ${dateLabel} / ${serviceLabel(doc.service)} / ${client?.name || ""}</div>
            </div>
            <span class="pill completed">届いた書類</span>
          </div>
          <div class="card-actions" style="margin-top:12px;">
            <button class="btn btn-primary btn-compact" type="button" onclick="sendClientEstimateRequestFromUser('user-estimate-request-notice-${doc.id}')">${estimateSent ? "見積依頼書を再送する" : "見積依頼書として返送する"}</button>
            <span class="pill ${estimateSent ? "completed" : "pending"}">${estimateSent ? "見積依頼書 返送済み" : "返送待ち"}</span>
          </div>
          <p id="user-estimate-request-notice-${doc.id}" class="inline-notice" hidden></p>
        </div>
      `;
    }).join("") : fallbackCard;
  }
  productList.innerHTML = group.items.map((item) => {
    const product = previewData().products?.[item.product];
    if (!product) return "";
    const current = getItemState(item.product, product.status || "認証待ち");
    const delivered = current.userDocumentDelivered || current.clientReturned;
    return `
      <div class="product-card">
        <div class="product-thumb"><img src="${product.image}" alt="${product.name}"></div>
        <div class="product-body">
          <div class="product-top">
            <div>
              <div class="product-title">${product.name}</div>
              <div class="product-meta">${serviceLabel(group.service)} / ${formatYen(current.soldPrice || item.price || 0)} / 送料 ${formatYen(current.shipping || 0)}</div>
            </div>
            <span class="pill ${statusClass(current.status || product.status)}">${current.status || product.status}</span>
          </div>
          <div class="detail-grid">
            <div class="field-block"><div class="field-label">書類反映</div><div class="field-value">${delivered ? "買取明細書に紐づき済み" : "未送付"}</div></div>
            <div class="field-block"><div class="field-label">通知</div><div class="field-value">${current.userNotificationUnread ? "未読通知あり" : "通知なし"}</div></div>
            <div class="field-block"><div class="field-label">商品詳細</div><div class="field-value"><a href="${product.detail_page}">${product.name} の詳細を見る</a></div></div>
            <div class="field-block"><div class="field-label">管理メモ</div><div class="field-value">${current.settlementNote || "未入力"}</div></div>
          </div>
        </div>
      </div>
    `;
  }).join("");
}

function updateUserItemStatus(productId, selectId, soldId, shippingId, noteId, noticeId) {
  const select = document.getElementById(selectId);
  const soldInput = document.getElementById(soldId);
  const shippingInput = document.getElementById(shippingId);
  const noteInput = document.getElementById(noteId);
  const product = previewData().products?.[productId];
  if (!select || !product) return;
  const soldPrice = Number(soldInput?.value || 0);
  const shipping = Number(shippingInput?.value || 0);
  const settlementNote = noteInput?.value || "";
  setItemState(productId, {
    status: select.value,
    unavailable: false,
    soldPrice,
    shipping,
    settlementNote,
  });
  showInlineNotice(noticeId, `${product.name} を「${select.value}」へ変更し、販売金額 ${formatYen(soldPrice)} / 送料 ${formatYen(shipping)} をユーザー商品一覧へ反映する想定です。`);
  updateStageTabCounts();
}

function renderBatchCreateMeta() {
  const title = document.getElementById("batch-create-title");
  if (!title) return;
  const copy = document.getElementById("batch-create-copy");
  const listTitle = document.getElementById("batch-create-list-title");
  const configTitle = document.getElementById("batch-create-config-title");
  const targetLabel = document.getElementById("batch-target-label");
  const docType = document.getElementById("document-type");
  const service = getQueryParam("service") || "wholesale";
  const config = {
    wholesale: {
      title: "業者販売の書類を作成する",
      copy: "業者販売へ流す商品を複数選択し、見積依頼書テンプレートへまとめて差し込みます。",
      listTitle: "業者販売へ流す商品",
      configTitle: "見積依頼書の設定",
      targetLabel: "送付先業者",
      docType: "見積依頼書",
    },
    auction: {
      title: "オークション依頼書を作成する",
      copy: "オークションへ出す商品を複数選択し、オークション依頼書テンプレートへ差し込みます。",
      listTitle: "オークションへ出す商品",
      configTitle: "オークション依頼書の設定",
      targetLabel: "送付先業者",
      docType: "オークション依頼書",
    },
    simultaneous: {
      title: "同時出品の管理書類を作成する",
      copy: "同時出品の商品を選択し、出品管理シートへ差し込みます。",
      listTitle: "同時出品で扱う商品",
      configTitle: "出品管理シートの設定",
      targetLabel: "出品先",
      docType: "出品管理シート",
    },
  }[service];
  if (!config) return;
  title.textContent = config.title;
  if (copy) copy.textContent = config.copy;
  if (listTitle) listTitle.textContent = config.listTitle;
  if (configTitle) configTitle.textContent = config.configTitle;
  if (targetLabel) targetLabel.textContent = config.targetLabel;
  if (docType) docType.value = config.docType;
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".js-product-card[data-product-id]").forEach((card) => {
    const productId = card.dataset.productId;
    const product = previewData().products?.[productId];
    const fallbackStatus = card.dataset.defaultStatus || product?.status || "認証待ち";
    const current = getItemState(productId, fallbackStatus);
    const badge = card.querySelector(`[id^="badge-"]`);
    const stateTarget = card.querySelector(`[id^="state-"]`);
    const select = card.querySelector("select.status-select");

    if (badge) {
      badge.textContent = current.status || fallbackStatus;
      badge.className = `pill ${statusClass(current.status || fallbackStatus)}`;
    }
    if (stateTarget) {
      stateTarget.textContent = current.status || fallbackStatus;
    }
    if (select) {
      select.value = current.status || fallbackStatus;
    }

    if (current.unavailable) {
      card.style.display = "none";
    }

    if (card.dataset.stage && card.dataset.stage.startsWith("outgoing-")) {
      const expected = card.dataset.expectedStatus || "査定中";
      if ((current.status || fallbackStatus) !== expected || current.unavailable || current.stage2Completed) {
        card.style.display = "none";
      }
    }
  });

  ["wholesale", "auction", "simultaneous"].forEach((service) => {
    const cards = Array.from(document.querySelectorAll(`.js-product-card[data-stage="outgoing-${service}"]`)).filter((card) => card.style.display !== "none");
    const empty = document.getElementById(`outgoing-empty-${service}`);
    if (empty) empty.hidden = cards.length > 0;
  });

  document.querySelectorAll(".vendor-check").forEach((checkbox) => {
    checkbox.addEventListener("change", syncVendorSelectionUI);
  });

  const filters = [
    ["client-incoming-search", ".summary-card.js-filter-card", "client-incoming-empty"],
    ["vendor-outgoing-search", ".product-card.js-filter-card", "vendor-outgoing-search-empty"],
    ["vendor-incoming-search", ".file-card.js-filter-card", "vendor-incoming-search-empty"],
    ["client-outgoing-search", ".js-client-group", "client-outgoing-empty"],
  ];
  filters.forEach(([inputId, selector, emptyId]) => {
    const input = document.getElementById(inputId);
    if (!input) return;
    input.addEventListener("input", () => applySearchFilter(inputId, selector, emptyId));
  });

  ["history-title-search", "history-month-filter", "history-date-from", "history-date-to"].forEach((inputId) => {
    const input = document.getElementById(inputId);
    if (!input) return;
    input.addEventListener("input", applyHistoryFilters);
    input.addEventListener("change", applyHistoryFilters);
  });

  const settlementMonth = document.getElementById("settlement-month-filter");
  if (settlementMonth) {
    settlementMonth.addEventListener("input", renderSettlementCurrentPage);
    settlementMonth.addEventListener("change", renderSettlementCurrentPage);
  }

  document.querySelectorAll(".history-tab[data-history-filter]").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".history-tab").forEach((node) => node.classList.remove("is-active"));
      tab.classList.add("is-active");
      renderCompletedDocumentHistory(tab.dataset.historyFilter || "all");
    });
  });

  document.querySelectorAll(".assign-input[data-product-id]").forEach((input) => {
    const assignment = getAssignments()[input.dataset.productId];
    if (assignment?.clientName) {
      input.value = assignment.clientName;
    }
  });

  syncVendorSelectionUI();
  updateStageTabCounts();
  renderBatchCreateMeta();
  renderVendorEstimateTemplate();
  renderStage4Summary();
  renderClientDelivery();
  renderStatementTemplate();
  renderUserItemEditor();
  renderUserDocumentsPreview();
  renderPendingClientEstimateRequests();
  renderSettlementCurrentPage();
  renderCalculationCurrentPage();
  renderCompletedDocumentHistory();
  applySearchFilter("client-incoming-search", ".summary-card.js-filter-card", "client-incoming-empty");
  applySearchFilter("vendor-outgoing-search", ".product-card.js-filter-card", "vendor-outgoing-search-empty");
  applySearchFilter("vendor-incoming-search", ".file-card.js-filter-card", "vendor-incoming-search-empty");
  applyHistoryFilters();
});
